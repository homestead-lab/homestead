"""Opt-in NFSv4 exports for the managed Network Shares inventory.

The NFS daemon runs in its own pod.  Nothing here owns or deletes a PVC:
shares and their data outlive the daemon's Deployment and Service.
"""
import copy
import ipaddress
import os
import re

import homestead_names as NAMES


NAME = NAMES.object_name("nfs")
IMAGE = os.environ.get("NFS_IMAGE", "pedroetb/nfs-server:v2.4.0")


def client_network(value):
    """Accept an explicit IPv4 client or CIDR, never an Internet-wide export."""
    try:
        network = ipaddress.IPv4Network(str(value or "").strip(), strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError) as error:
        raise ValueError("NFS clients must be an IPv4 address or CIDR such as 192.0.2.0/24") from error
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
        selected.append({"name": name, "pvc": row["pvc"], "sub_path": sub_path,
                         "clients": clients, "read_only": bool(row.get("nfs_read_only", True))})
    return selected


def configure(deployment, selected):
    """Write only the NFS pod's mounts and exports, preserving its lifecycle."""
    if not selected:
        raise ValueError("choose at least one NFS export in Network Shares before enabling NFS")
    dep = copy.deepcopy(deployment)
    spec = dep["spec"]["template"]["spec"]
    container = spec["containers"][0]
    container["image"] = IMAGE
    container["securityContext"] = {"capabilities": {"add": ["SYS_ADMIN"]}}
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
                    f"{path} {row['clients']}({mode},sync,fsid={index},root_squash,no_subtree_check)"})
        name = f"export-{index}"
        mount = {"name": name, "mountPath": path, "readOnly": row["read_only"]}
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
    return (left["containers"][0].get("image") == right["containers"][0].get("image")
            and left["containers"][0].get("env") == right["containers"][0].get("env")
            and left["containers"][0].get("volumeMounts") == right["containers"][0].get("volumeMounts")
            and left.get("volumes") == right.get("volumes"))
