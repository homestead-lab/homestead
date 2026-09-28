"""Opt-in NFSv4 exports for the managed Network Shares inventory.

The NFS daemon runs in its own pod.  Nothing here owns or deletes a PVC:
shares and their data outlive the daemon's Deployment and Service.
"""
import homestead_specs as SPECS
import copy
import ipaddress
import os
import re
import uuid

import homestead_names as NAMES
import homestead_failover as FAILOVER


NAME = NAMES.object_name("nfs")
IMAGE = os.environ.get("NFS_IMAGE", "pedroetb/nfs-server:v2.4.0")
HOST_LABEL = NAMES.key("nfs-server-ready")


def export_ids(rows, deployment=None):
    """Persist export identities, adopting v2.8.157's live numeric IDs once.

    Deriving fsid from list position changes clients' file handles whenever a
    preceding share is added or removed. Keep the live identity during upgrade
    and give new exports a UUID that survives edits and server removal.
    """
    live = {}
    spec = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    for container in spec.get("containers") or []:
        for env in container.get("env") or []:
            if not env.get("name", "").startswith("NFS_EXPORT_"):
                continue
            match = re.search(r"^/exports/([a-z0-9-]+)\s+.*\bfsid=([a-fA-F0-9-]+)[,)]", env.get("value", ""))
            if match:
                live[match[1]] = match[2]
    result = copy.deepcopy(rows)
    for row in result:
        if row.get("nfs_clients") and not row.get("nfs_fsid"):
            row["nfs_fsid"] = live.get(row["name"]) or str(uuid.uuid4())
    return result


def _fsid(row):
    value = str(row.get("nfs_fsid") or uuid.uuid5(uuid.NAMESPACE_URL,
        f"homestead:nfs:{row['pvc']}:{row['name']}:{row.get('sub_path', '')}"))
    if value.isdecimal() and int(value) > 0:
        return value
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError("NFS export identity is invalid") from error


def client_network(value):
    """Accept an explicit IPv4 client or CIDR, never an Internet-wide export."""
    try:
        network = ipaddress.IPv4Network(str(value or "").strip(), strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as error:
        raise ValueError("NFS clients must be an IPv4 address or CIDR such as 192.168.1.0/24") from error
    if network.prefixlen == 0 or network.is_multicast or network.is_loopback or network.is_unspecified:
        raise ValueError("NFS clients must be a specific reachable IPv4 address or network, not everyone")
    return str(network)


def exports(rows, get_pvc):
    """Validate explicit export choices and the independent RWX mounts."""
    selected = []
    for row in sorted(rows, key=lambda item: item.get("name", "")):
        raw = str(row.get("nfs_clients") or "").strip()
        if not raw:
            continue
        name = row.get("name", "")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?", str(name)) or not row.get("pvc"):
            raise ValueError("an NFS export needs a named share backed by a PVC")
        sub_path = str(row.get("sub_path") or "")
        if sub_path and (not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", sub_path)
                         or ".." in sub_path.split("/")):
            raise ValueError(f"{name}: NFS folder must stay inside its volume")
        clients = client_network(raw)
        pvc = get_pvc(row["pvc"])
        access = ((pvc.get("spec") or {}).get("accessModes") or [])
        if "ReadWriteMany" not in access:
            raise ValueError(f"{name}: NFS needs an RWX claim so its separate pod can mount the data safely")
        if (pvc.get("status") or {}).get("phase") != "Bound":
            raise ValueError(f"{name}: NFS volume {row['pvc']} is not Bound")
        selected.append({"name": name, "pvc": row["pvc"], "sub_path": sub_path, "fsid": _fsid(row),
                         "clients": clients, "read_only": bool(row.get("nfs_read_only", True))})
    return selected


def configure(deployment, selected):
    """Write only the NFS pod's mounts and exports, preserving its lifecycle."""
    if not selected:
        raise ValueError("choose at least one NFS export in Network Shares before enabling NFS")
    dep = copy.deepcopy(deployment)
    spec = dep["spec"]["template"]["spec"]
    # A kernel NFS server is a singleton. The Deployment can replace it on a
    # surviving Linux host, but only where the probe confirms nfsd support.
    dep["spec"]["strategy"] = {"type": "Recreate"}
    if int(dep["spec"].get("replicas", 1) or 0) > 0:
        dep["spec"]["replicas"] = 1
    spec["hostname"] = NAME
    spec.setdefault("nodeSelector", {})["kubernetes.io/os"] = "linux"
    spec["nodeSelector"][HOST_LABEL] = "true"
    FAILOVER.apply(spec, "move")
    container = spec["containers"][0]
    container["image"] = IMAGE
    container["securityContext"] = {"capabilities": {"add": ["SYS_ADMIN"]}}
    container["startupProbe"] = {"tcpSocket": {"port": 2049}, "periodSeconds": 5, "failureThreshold": 60}
    container["readinessProbe"] = {"tcpSocket": {"port": 2049}, "periodSeconds": 5, "timeoutSeconds": 2,
                                   "failureThreshold": 2}
    # NFSv4 needs one pseudo-root (fsid=0) and child exports below it.
    networks = sorted({row["clients"] for row in selected})
    root_clients = " ".join(
        f"{network}(ro,sync,fsid=0,root_squash,no_subtree_check)" for network in networks)
    env = [{"name": "NFS_DISABLE_VERSION_3", "value": "1"},
           {"name": "NFS_EXPORT_0", "value": f"/exports {root_clients}"}]
    mounts = [{"name": "nfs-root", "mountPath": "/exports"}]
    volumes = [{"name": "nfs-root", "emptyDir": {}}]
    for index, row in enumerate(selected, 1):
        path = f"/exports/{row['name']}"
        mode = "ro" if row["read_only"] else "rw"
        env.append({"name": f"NFS_EXPORT_{index}", "value":
                    f"{path} {row['clients']}({mode},sync,fsid={row.get('fsid') or _fsid(row)},root_squash,no_subtree_check)"})
        name = f"export-{index}"
        mount = {"name": name, "mountPath": path}
        if row["read_only"]:
            mount["readOnly"] = True
        if row["sub_path"]:
            mount["subPath"] = row["sub_path"]
        mounts.append(mount)
        volumes.append({"name": name, "persistentVolumeClaim": {"claimName": row["pvc"]}})
    container["env"] = env
    container["volumeMounts"] = mounts
    spec["volumes"] = volumes
    return dep


def same_pod_config(a, b):
    left = a["spec"]["template"]["spec"]
    right = b["spec"]["template"]["spec"]
    fields = ("image", "env", "volumeMounts", "securityContext", "startupProbe", "readinessProbe")
    # Without the zero values Kubernetes leaves out when it stores a spec,
    # or every check finds drift and restarts NFS (homestead_specs).
    return (all(SPECS.same(left["containers"][0].get(key), right["containers"][0].get(key)) for key in fields)
            and all(SPECS.same(left.get(key), right.get(key)) for key in ("volumes", "hostname", "nodeSelector", "tolerations"))
            and SPECS.same(a["spec"].get("strategy"), b["spec"].get("strategy"))
            and a["spec"].get("replicas") == b["spec"].get("replicas"))


def recovery_report(nodes, volumes, replicas, platform, node_down, errors=(), podspec=None):
    """Report observed recovery prerequisites, never infer HA from RWX alone."""
    blockers, warnings = [], list(errors)
    ready = lambda node: any(c.get("type") == "Ready" and c.get("status") == "True"
                             for c in (node.get("status") or {}).get("conditions") or [])
    labels = lambda node: (node.get("metadata") or {}).get("labels") or {}
    hosts = []
    selectors = {**((podspec or {}).get("nodeSelector") or {}), HOST_LABEL: "true"}
    for node in nodes:
        spec = node.get("spec") or {}
        if (ready(node) and not spec.get("unschedulable")
                and not any(t.get("effect") in ("NoSchedule", "NoExecute") for t in spec.get("taints") or [])
                and all(labels(node).get(key) == value for key, value in selectors.items())):
            hosts.append(node["metadata"]["name"])
    if len(hosts) < 2:
        blockers.append(f"Only {len(hosts)} Ready, schedulable host(s) have verified NFS server support; two are needed for a replacement host.")
    controls = [n for n in nodes if any(key in labels(n) for key in (
        "node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))]
    if len([n for n in controls if ready(n)]) < 2:
        blockers.append("Fewer than two Ready control-plane nodes: losing the only API/scheduler host prevents automatic recovery.")
    etcd = [n for n in nodes if "node-role.kubernetes.io/etcd" in labels(n)]
    if etcd and sum(ready(n) for n in etcd) - (len(etcd) // 2 + 1) < 1:
        blockers.append("The visible etcd members cannot lose one member and keep quorum.")
    elif not etcd:
        warnings.append("External datastore/quorum resilience has not been verified.")
    if platform.get("load_balancer") not in ("kube-vip", "metallb"):
        blockers.append("NFS needs a load balancer that moves its VIP to a node with a Ready local endpoint.")
    elif platform.get("load_balancer") == "kube-vip" and platform.get("vip_service_election") is not True:
        warnings.append("kube-vip per-Service election is not verified; Local traffic requires it for VIP failover.")
    if node_down not in ("delete-deployment-pod", "delete-both-statefulset-and-deployment-pod"):
        warnings.append("Longhorn automatic deletion of pods on failed nodes is not enabled or not verified; recovery may wait for intervention.")
    storage = []
    ready_nodes = {node["metadata"]["name"] for node in nodes if ready(node)}
    for item in volumes:
        volume = item.get("volume")
        if not volume:
            warnings.append(f"{item['pvc']}: replicated storage could not be verified.")
            continue
        name = (volume.get("metadata") or {}).get("name")
        healthy = sorted({(r.get("spec") or {}).get("nodeID") for r in replicas or []
            if (r.get("spec") or {}).get("volumeName") == name
            and (r.get("spec") or {}).get("healthyAt") and not (r.get("spec") or {}).get("failedAt")
            and (r.get("status") or {}).get("currentState") == "running"
            and (r.get("spec") or {}).get("nodeID") in ready_nodes})
        storage.append({"pvc": item["pvc"], "healthy_replica_nodes": healthy,
                        "desired_replicas": (volume.get("spec") or {}).get("numberOfReplicas", 0)})
        if replicas is None:
            warnings.append(f"{item['pvc']}: actual replica health is unavailable.")
        elif len(healthy) < 2:
            blockers.append(f"{item['pvc']}: healthy replicas are on {len(healthy)} host(s); losing the only copy makes the export unavailable.")
    warnings.append("Longhorn RWX re-exports do not support NFS file locks/delegations or normal lock recovery. Use this gateway for ordinary files, not VM disks or databases requiring locks.")
    return {"level": "blocked" if blockers else "limited", "blockers": blockers, "warnings": warnings,
            "eligible_hosts": sorted(hosts), "volumes": storage, "lock_recovery": False,
            "detail": "Automatic recovery has blockers" if blockers else "Reconnect recovery prerequisites checked; lock recovery is unsupported"}
