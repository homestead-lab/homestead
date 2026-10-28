"""The servers network shares are served from: Samba, and the opt-in NFS gateway.

homestead_shares keeps the share inventory - names, volumes, users. This is
the workloads that serve it: installing Samba (and moving an older "samba"
workload to Homestead's name), switching it on or off, repairing it, and the
NFS gateway with its exports. Both live in the namespace shares do.
"""
import copy
import os
import time

import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_network_services as SVC
import homestead_nfs as NFS
import homestead_operations as OPS
import homestead_platform as PLATFORM
import homestead_shares as SHARES

kget = ksend = build_deployment = None
shared_classes = default_class = storage_options = None
_cache = {}
SMB_NAMESPACE = "lab"
SAMBA_IMAGE = os.environ.get("SAMBA_IMAGE", "dperson/samba:latest")
SMB_NAME = NAMES.object_name("smb")


def bind(_kget, _ksend, _build_deployment, namespace, cache,
         _shared_classes=None, _default_class=None, _storage_options=None):
    """The last three are server.py's: the classes that can share a volume
    between nodes, the class new volumes go on, and what a new share can be
    created on - all read from the classes server.py keeps track of."""
    global kget, ksend, build_deployment, SMB_NAMESPACE, _cache
    global shared_classes, default_class, storage_options
    kget, ksend, build_deployment = _kget, _ksend, _build_deployment
    SMB_NAMESPACE, _cache = namespace, cache
    shared_classes, default_class, storage_options = _shared_classes, _default_class, _storage_options


def _smb_path(kind, name):
    return f"/api{'/v1' if kind == 'services' else 's/apps/v1'}/namespaces/{SMB_NAMESPACE}/{kind}/{name}"


def _smb_new_object(old, name):
    """Prepare a copied Kubernetes object for POST under its new name."""
    previous_address = next((item.get("ip", "") for item in
                             ((old.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []), "")
    body = copy.deepcopy(old)
    body.pop("status", None)
    meta = body.setdefault("metadata", {})
    for field in ("resourceVersion", "uid", "generation", "creationTimestamp", "managedFields",
                  "selfLink", "deletionTimestamp", "deletionGracePeriodSeconds", "ownerReferences"):
        meta.pop(field, None)
    meta["name"] = name
    meta.setdefault("labels", {})["app"] = name
    meta["labels"][NAMES.key("managed")] = "true"
    if body.get("kind") == "Service":
        body["spec"]["selector"] = {"app": name}
        annotations = meta.setdefault("annotations", {})
        if (previous_address and body["spec"].get("type") == "LoadBalancer"
                and not any(key in annotations for key in
                            ("kube-vip.io/loadbalancerIPs", "metallb.universe.tf/loadBalancerIPs"))
                and not body["spec"].get("loadBalancerIP")):
            annotations.update(PLATFORM.vip_annotations(previous_address))
        for field in ("clusterIP", "clusterIPs", "ipFamilies", "healthCheckNodePort"):
            body["spec"].pop(field, None)
        for port in body["spec"].get("ports", []) or []:
            port.pop("nodePort", None)
    else:
        body["spec"]["selector"] = {"matchLabels": {"app": name}}
        body["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})["app"] = name
        body["spec"]["template"]["spec"]["containers"][0]["name"] = name
    return body


def _migrate_samba(legacy):
    with SHARES.LOCK:
        return _migrate_samba_locked(legacy)


def _migrate_samba_locked(legacy):
    """Recreate the legacy workload under Homestead's name and preserve its VIP.

    The replacement starts at zero replicas so RWO claims are never mounted by
    both pods. If cutover fails, the old Deployment and Service are restored.
    """
    old_dep_path = _smb_path("deployments", "samba")
    new_dep_path = _smb_path("deployments", SMB_NAME)
    old_svc_path = _smb_path("services", "samba")
    new_svc_path = _smb_path("services", SMB_NAME)
    old_service = KUBE.get_optional(old_svc_path, read=kget)
    rows, credentials, *_ = SHARES._state()
    new_dep = _smb_new_object(legacy, SMB_NAME)
    replicas = int((legacy.get("spec") or {}).get("replicas", 1) or 0)
    new_dep["spec"]["replicas"] = 0
    new_dep = SHARES.configured_deployment(new_dep, rows, credentials)
    if old_service:
        new_service = _smb_new_object(old_service, SMB_NAME)
    else:
        cfg = {"name": SMB_NAME, "container_name": SMB_NAME, "image": SAMBA_IMAGE,
               "namespace": SMB_NAMESPACE, "ports": [{"container": 445, "name": "smb", "expose": True}],
               "vip_mode": "automatic"}
        cfg = SVC.prepare_deploy(cfg)
        _, new_service = build_deployment(cfg)
    made_dep = removed_service = made_service = stopped_old = False
    try:
        ksend("POST", f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments", new_dep)
        made_dep = True
        if old_service:
            ksend("DELETE", old_svc_path)
            removed_service = True
        ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", new_service)
        made_service = True
        if replicas:
            ksend("PATCH", old_dep_path, {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
            stopped_old = True
            ksend("PATCH", new_dep_path, {"spec": {"replicas": replicas}},
                  ctype="application/merge-patch+json")
            deadline = time.time() + SHARES.ROLLOUT_TIMEOUT
            while time.time() < deadline:
                live = KUBE.get_optional(new_dep_path, read=kget) or {}
                status = live.get("status") or {}
                if (int(status.get("readyReplicas", 0) or 0) >= replicas and
                        int(status.get("updatedReplicas", 0) or 0) >= replicas and
                        int(status.get("observedGeneration", 0) or 0) >=
                        int((live.get("metadata") or {}).get("generation", 0) or 0)):
                    break
                time.sleep(1.5)
            else:
                raise ValueError("homestead-smb did not become ready; the former Samba workload was restored")
        ksend("DELETE", old_dep_path)
    except Exception as error:
        recovery = []
        for action in (
            lambda: ksend("PATCH", new_dep_path, {"spec": {"replicas": 0}},
                          ctype="application/merge-patch+json") if made_dep else None,
            lambda: ksend("DELETE", new_svc_path) if made_service else None,
            lambda: ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services",
                          _smb_new_object(old_service, "samba")) if removed_service else None,
            lambda: ksend("PATCH", old_dep_path, {"spec": {"replicas": replicas}},
                          ctype="application/merge-patch+json") if stopped_old else None,
            lambda: ksend("DELETE", new_dep_path) if made_dep else None,
        ):
            try:
                action()
            except Exception as failure:
                recovery.append(str(failure)[:120])
        if recovery:
            raise RuntimeError(f"SMB migration failed: {error}; recovery needs attention: "
                               + "; ".join(recovery)) from error
        raise
    _cache.pop("wl", None); _cache.pop("network", None)
    return kget(new_dep_path)


def install_samba(address=""):
    """The Samba server shares are served from, made when the first share is:
    SMB on port 445 at an address of its own - the one chosen, or the next
    free one. Shares are its arguments, which Homestead writes, so it starts
    with none."""
    existing = KUBE.get_optional(_smb_path("deployments", SMB_NAME), read=kget)
    if existing:
        return existing
    legacy = KUBE.get_optional(_smb_path("deployments", "samba"), read=kget)
    if legacy:
        return _migrate_samba(legacy)
    cfg = {"name": SMB_NAME, "container_name": SMB_NAME, "image": SAMBA_IMAGE, "namespace": SMB_NAMESPACE,
           "ports": [{"container": 445, "name": "smb", "protocol": "TCP", "expose": True}],
           "vip_mode": "manual" if address else "automatic", "lb_ip": address,
           "args": ["-g", "server min protocol = SMB2"],
           "env": {**SHARES.filesystem_identity(), "PERMISSIONS": ""}}
    cfg = SVC.prepare_deploy(cfg)
    dep, svc = build_deployment(cfg)
    created = ksend("POST", f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments", dep)
    if svc:
        try:
            ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", svc)
        except Exception:
            ksend("DELETE", _smb_path("deployments", SMB_NAME))
            raise
    _cache.pop("wl", None); _cache.pop("network", None)
    return created


def samba_state():
    """The Samba server shares are served from: whether it runs, and where."""
    dep = KUBE.get_optional(_smb_path("deployments", SMB_NAME), read=kget) or KUBE.get_optional(
        _smb_path("deployments", "samba"), read=kget)
    try:
        rows, credentials, *_ = SHARES._state(tolerant=True)
        configured = {row["name"] for row in rows}
        config_error = ""
    except Exception as error:
        configured = set()
        credentials = {}
        rows = []
        config_error = str(error)[:160]
    if not dep:
        return {"installed": False, "enabled": False, "shares": len(configured), "image": SAMBA_IMAGE,
                "name": SMB_NAME, "served_shares": [], "in_sync": not configured and not config_error,
                **({"error": config_error} if config_error else {})}
    name = dep["metadata"]["name"]
    status = dep.get("status") or {}
    address = ""
    try:
        svc = KUBE.get_optional(_smb_path("services", SMB_NAME), read=kget) or KUBE.get_optional(
            _smb_path("services", "samba"), read=kget) or {}
        address = next((i.get("ip", "") for i in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []), "") \
            or ((svc.get("metadata") or {}).get("annotations") or {}).get("kube-vip.io/loadbalancerIPs", "") \
            or ((svc.get("metadata") or {}).get("annotations") or {}).get("metallb.universe.tf/loadBalancerIPs", "") \
            or ((svc.get("spec") or {}).get("loadBalancerIP") or "")
    except Exception:
        pass
    containers = ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or [{}]
    container = containers[0]
    paths = {mount.get("mountPath") for mount in container.get("volumeMounts", []) or []}
    args = container.get("args") or []
    served = {arg.split(";", 1)[0] for index, flag in enumerate(args[:-1]) if flag == "-s"
              for arg in [args[index + 1]] if ";" in arg and arg.split(";", 2)[1] in paths}
    try:
        recovery = SHARES.RECOVERY.read(dep)
        offline = SHARES.RECOVERY.offline_shares(dep, rows)
    except Exception as error:
        recovery, offline = {}, []
        config_error = str(error)[:160]
    expected_names = configured - {item["name"] for item in offline}
    in_sync = served == expected_names and not config_error and name == SMB_NAME
    if in_sync:
        try:
            expected = SHARES.configured_deployment(dep, rows, credentials)
            expected_spec = expected["spec"]["template"]["spec"]
            live_spec = dep["spec"]["template"]["spec"]
            expected_container = expected_spec["containers"][0]
            in_sync = (all(SHARES.SPECS.same(container.get(field), expected_container.get(field))
                           for field in ("args", "env", "volumeMounts"))
                       and live_spec.get("volumes", []) == expected_spec.get("volumes", [])
                       and SHARES.SPECS.same(live_spec.get("initContainers", []), expected_spec.get("initContainers", []))
                       and container.get("image") == SAMBA_IMAGE)
        except Exception as error:
            config_error = str(error)[:160]
            in_sync = False
    desired = int((dep.get("spec") or {}).get("replicas", 1) or 0)
    return {"installed": True, "enabled": desired > 0, "desired": desired, "name": name,
            "ready": int(status.get("readyReplicas", 0) or 0), "address": address,
            "shares": len(configured), "served_shares": sorted(served), "in_sync": in_sync,
            "offline_shares": offline, "partial": bool(offline),
            "recovery_pending": recovery.get("pending", {}), "recovery_warning": recovery.get("warning", ""),
            "recovery_failures": recovery.get("restore_failures", {}),
            "image": container.get("image", ""), **({"error": config_error} if config_error else {})}


def set_samba(enabled, address=""):
    """Samba on or off. Off stops serving; every share, its volume and its
    password are kept for when it is switched back on. On installs it first
    if the cluster has none, with the shares already defined."""
    state = samba_state()
    name = state["name"]
    if not enabled:
        if state["installed"]:
            ksend("PATCH", _smb_path("deployments", name), {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
        _cache.pop("wl", None)
        return {"ok": True, "detail": "Samba is stopping; the shares, their volumes and passwords are kept"}
    if not state["installed"]:
        install_samba(address)
        rows, credentials, *_ = SHARES._state()
        if rows:
            SHARES.apply_samba(rows, credentials)
        _cache.pop("wl", None)
        return {"ok": True, "detail": "Samba is being installed" + (f" with {len(rows)} share{'s' if len(rows) != 1 else ''}" if rows else "")}
    if name == "samba":
        install_samba()
        name = SMB_NAME
    ksend("PATCH", _smb_path("deployments", name), {"spec": {"replicas": 1}},
          ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "detail": "Samba is starting"}


def remove_samba():
    """Uninstall only the SMB workload and address; never touch share data."""
    with SHARES.LOCK:
        removed = []
        for name in (SMB_NAME, "samba"):
            for kind in ("deployments", "services"):
                path = _smb_path(kind, name)
                if KUBE.get_optional(path, read=kget) is not None:
                    ksend("DELETE", path)
                    removed.append(f"{kind}/{name}")
        _cache.pop("wl", None)
        _cache.pop("network", None)
    return {"ok": True, "removed": removed,
            "detail": "SMB server removed. Share definitions, passwords, PVCs and their data were kept."}


def repair_samba(address=""):
    """Apply the saved inventory now, with the same rollout guard as edits."""
    with SHARES.LOCK:
        install_samba(address)
        result = SHARES.reconcile_samba(SAMBA_IMAGE, retry_recovery=True)
    _cache.pop("wl", None)
    return {"ok": True, "detail": "SMB mappings checked; recovered storage must pass the stability check before shares return" if result["state"] == "current"
            else "SMB is being repaired from the saved shares", "server": samba_state()}


def nfs_state():
    """The opt-in NFSv4 gateway is independent of the SMB server and PVCs."""
    dep = KUBE.get_optional(_smb_path("deployments", NFS.NAME), read=kget)
    svc = KUBE.get_optional(_smb_path("services", NFS.NAME), read=kget)
    rows, *_ = SHARES._state(tolerant=True)
    names = [row["name"] for row in rows if row.get("nfs_clients")]
    desired = int(((dep or {}).get("spec") or {}).get("replicas", 0) or 0)
    ingress = ((((svc or {}).get("status") or {}).get("loadBalancer") or {}).get("ingress") or [])
    address = next((item.get("ip", "") for item in ingress if item.get("ip")), "")
    annotations = (((svc or {}).get("metadata") or {}).get("annotations") or {})
    address = address or annotations.get("kube-vip.io/loadbalancerIPs", "") or annotations.get(
        "metallb.universe.tf/loadBalancerIPs", "")
    return {"installed": bool(dep), "enabled": bool(dep) and desired > 0,
            "name": NFS.NAME, "image": NFS.IMAGE, "exports": names, "address": address,
            "ready": int(((dep or {}).get("status") or {}).get("readyReplicas", 0) or 0),
            "desired": desired, "recovery": nfs_recovery(rows, dep)}


def nfs_recovery(rows, deployment=None):
    errors, volumes = [], []
    def read(path, default, label):
        try:
            return kget(path)
        except Exception:
            errors.append(f"{label} could not be checked.")
            return default
    nodes = read("/api/v1/nodes", {}, "Node availability").get("items", [])
    lh = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
    replicas = read(f"{lh}/replicas", {}, "Storage replicas").get("items")
    policy = read(f"{lh}/settings/node-down-pod-deletion-policy", {}, "Longhorn node recovery").get("value")
    for pvc in sorted({row["pvc"] for row in rows if row.get("nfs_clients") and row.get("pvc")}):
        claim = read(f"/api/v1/namespaces/{SMB_NAMESPACE}/persistentvolumeclaims/{pvc}", {}, pvc)
        name = (claim.get("spec") or {}).get("volumeName")
        volume = read(f"{lh}/volumes/{name}", None, f"{pvc} replication") if name else None
        volumes.append({"pvc": pvc, "volume": volume})
    try:
        platform = PLATFORM.detect()
    except Exception:
        platform = {}
        errors.append("Load balancer availability could not be checked.")
    podspec = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return NFS.recovery_report(nodes, volumes, replicas, platform, policy, errors, podspec)


def _nfs_inventory(current=None):
    rows, _, config, _, _ = SHARES._state(tolerant=True)
    identified = NFS.export_ids(rows, current)
    if identified != rows:
        SHARES._save_config(identified, config)
    return identified


def _nfs_exports(rows):
    return NFS.exports(rows, SHARES._pvc)


def _nfs_deployment(rows, address=""):
    selected = _nfs_exports(rows)
    if not selected:
        raise ValueError("choose at least one NFS export in Network Shares first")
    if PLATFORM.detect().get("load_balancer") == "servicelb":
        raise ValueError("NFS needs a dedicated VIP that preserves client IPs; install kube-vip under Cluster Add-ons first")
    cfg = {"name": NFS.NAME, "container_name": NFS.NAME, "namespace": SMB_NAMESPACE,
           "image": NFS.IMAGE, "cpu": "50m", "memory": "128Mi",
           "ports": [{"container": 2049, "name": "nfs", "protocol": "TCP", "expose": True}],
           "vip_mode": "manual" if address else "automatic", "lb_ip": address, "exclusive_vip": True}
    cfg = SVC.prepare_deploy(cfg)
    dep, svc = build_deployment(cfg)
    dep = NFS.configure(dep, selected)
    if svc:
        # NFS's CIDR rules must see the real client address, not a node SNAT.
        svc["spec"]["externalTrafficPolicy"] = "Local"
        svc["metadata"].setdefault("annotations", {})[NAMES.key("exclusive-vip")] = "true"
    return dep, svc


@SHARES.serialized
def reconcile_nfs(rows=None):
    """Keep a running NFS gateway's exports aligned with the saved inventory."""
    dep_path = _smb_path("deployments", NFS.NAME)
    current = KUBE.get_optional(dep_path, read=kget)
    if not current:
        return {"state": "absent"}
    if rows is None:
        rows = _nfs_inventory(current)
    selected = _nfs_exports(rows)
    if not selected:
        if int((current.get("spec") or {}).get("replicas", 0) or 0):
            ksend("PATCH", dep_path, {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
        return {"state": "off", "exports": 0}
    wanted = NFS.configure(current, selected)
    if NFS.same_pod_config(current, wanted):
        return {"state": "current", "exports": len(selected)}
    ksend("PUT", dep_path, wanted)
    _cache.pop("wl", None)
    return {"state": "updated", "exports": len(selected)}


def set_nfs(enabled, address=""):
    with SHARES.LOCK:
        path = _smb_path("deployments", NFS.NAME)
        current = KUBE.get_optional(path, read=kget)
        if not enabled:
            if current:
                ksend("PATCH", path, {"spec": {"replicas": 0}},
                      ctype="application/merge-patch+json")
            _cache.pop("wl", None)
            return {"ok": True, "detail": "NFS stopped; exports, shares and every PVC were kept"}
        rows = _nfs_inventory(current)
        _nfs_exports(rows)
        if not any(row.get("nfs_clients") for row in rows):
            raise ValueError("choose at least one NFS export in Network Shares first")
        if not current:
            dep, svc = _nfs_deployment(rows, address)
            ksend("POST", f"/apis/apps/v1/namespaces/{SMB_NAMESPACE}/deployments", dep)
            try:
                ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", svc)
            except Exception:
                ksend("DELETE", path)
                raise
        else:
            reconcile_nfs(rows)
            if KUBE.get_optional(_smb_path("services", NFS.NAME), read=kget) is None:
                _, svc = _nfs_deployment(rows, address)
                ksend("POST", f"/api/v1/namespaces/{SMB_NAMESPACE}/services", svc)
            ksend("PATCH", path, {"spec": {"replicas": 1}},
                  ctype="application/merge-patch+json")
        _cache.pop("wl", None)
        return {"ok": True, "detail": "NFS is starting; exports use NFSv4/TCP on port 2049"}


def remove_nfs():
    """Remove only the NFS address and daemon, never a claim or share record."""
    with SHARES.LOCK:
        current = KUBE.get_optional(_smb_path("deployments", NFS.NAME), read=kget)
        if current:
            _nfs_inventory(current)
        removed = []
        for kind in ("deployments", "services"):
            path = _smb_path(kind, NFS.NAME)
            if KUBE.get_optional(path, read=kget) is not None:
                ksend("DELETE", path)
                removed.append(f"{kind}/{NFS.NAME}")
        _cache.pop("wl", None)
        _cache.pop("network", None)
    return {"ok": True, "removed": removed,
            "detail": "NFS server removed. Export settings, SMB shares, PVCs and data were kept."}


def set_nfs_export(name, clients, read_only=True):
    """Save one explicit export; an empty client network removes that export."""
    with SHARES.LOCK:
        rows, _, config_obj, _, _ = SHARES._state(tolerant=True)
        rows = NFS.export_ids(rows, KUBE.get_optional(_smb_path("deployments", NFS.NAME), read=kget))
        row = next((item for item in rows if item.get("name") == name), None)
        if row is None:
            raise ValueError("share not found")
        previous = copy.deepcopy(rows)
        if clients:
            row["nfs_clients"] = NFS.client_network(clients)
            row["nfs_read_only"] = bool(read_only)
        else:
            row.pop("nfs_clients", None)
            row.pop("nfs_read_only", None)
        _nfs_exports(rows)
        config_path = f"/api/v1/namespaces/{SMB_NAMESPACE}/configmaps/{SHARES.CONFIGMAP()}"
        written = SHARES._save_config(rows, config_obj)
        try:
            reconcile_nfs(rows)
        except Exception:
            SHARES._restore_object(config_path, config_obj, written)
            try:
                reconcile_nfs(previous)
            except Exception:
                pass
            raise
        return {"ok": True, "exports": nfs_state()["exports"],
                "detail": f"NFS export for {name} {'set' if clients else 'removed'}; its PVC was kept"}


# Changing a share restarts the Samba server that serves it, and removing one
# drops its NFS export first, so the share routes live with the servers.
def _restart(kind, title, detail, **ref):
    return OPS.start(kind, title, {"kind": "Deployment", "name": SMB_NAME, "namespace": SMB_NAMESPACE},
                     "/shares", {"namespace": SMB_NAMESPACE, "name": SMB_NAME, "undo": "keep", **ref}, detail)


def create_share(b):
    access = b.get("access_mode")
    if access == "ReadWriteMany" and (b.get("storage_class") or default_class()) not in shared_classes():
        # One SMB server mounts every share: a class that cannot
        # serve many nodes (k3s's local-path) still serves it.
        access = "ReadWriteOnce"
    result = SHARES.create_share(
        b["name"], b.get("size_gb", 10), b.get("user", "lab"),
        b.get("password"), b.get("public", False), b.get("read_only", False),
        b.get("pvc"), b.get("sub_path", ""), b.get("storage_class"),
        access, b.get("new_name", ""), str(b.get("samba_ip") or "").strip(), b.get("account_mode"))
    deployment = result.pop("deployment", None)
    if deployment:
        result["operation"] = _restart("deployment", f"Create share {b['name']}", "Restarting Samba with the new share")
    return {"ok": True, **result}


def save_user(b):
    result = SHARES.save_user(b.get("user"), b.get("password"), b.get("action", "create"))
    if result.pop("deployment", None):
        result["operation"] = _restart("deployment", f"Update SMB user {result['user']}",
                                       "Restarting Samba with updated credentials")
    return {"ok": True, **result}


def edit_share(b):
    result = SHARES.edit_share(
        b["name"], b.get("size_gb"), b.get("user", "lab"),
        b.get("password"), b.get("public", False), b.get("read_only", False))
    deployment = result.pop("deployment", None)
    if deployment:
        result["operation"] = _restart("deployment", f"Update share {b['name']}", "Restarting Samba with updated access")
    return {"ok": True, **result}


def delete_share(b):
    share = next((row for row in SHARES.list_shares() if row.get("name") == b["name"]), None)
    old_export = (share or {}).get("nfs_clients", "")
    if old_export:
        set_nfs_export(b["name"], "")
    try:
        result = SHARES.delete_share(b["name"])
    except Exception:
        if old_export:
            set_nfs_export(b["name"], old_export, share.get("nfs_read_only", True))
        raise
    deployment = result.pop("deployment", None)
    if deployment:
        result["operation"] = _restart("share-remove", f"Remove share {b['name']}",
                                       "Restarting Samba without the removed share",
                                       uid=deployment.get("metadata", {}).get("uid"), since=time.time(),
                                       removed_claims=result.get("removed_claims", []))
    return {"ok": True, **result}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/shares/server"): ("viewer", lambda request: samba_state()),
    ("GET", "/api/shares/nfs/server"): ("viewer", lambda request: nfs_state()),
    ("GET", "/api/shares/options"): ("admin", lambda request: storage_options()),
    ("POST", "/api/shares"): ("admin", lambda request: create_share(request.body)),
    ("POST", "/api/shares/users"): ("admin", lambda request: save_user(request.body)),
    ("POST", "/api/shares/edit"): ("admin", lambda request: edit_share(request.body)),
    ("POST", "/api/shares/delete"): ("admin", lambda request: delete_share(request.body)),
    ("POST", "/api/shares/nfs"): ("admin", lambda request: set_nfs_export(
        request.body.get("name", ""), str(request.body.get("clients") or "").strip(),
        request.body.get("read_only") is not False)),
    ("POST", "/api/shares/repair"): ("admin", lambda request: repair_samba(str(request.body.get("address") or "").strip())),
}
