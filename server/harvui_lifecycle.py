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

# Remembers the replica count a workload should return to when autostart is
# switched back on, because scaling to zero forgets it.
AUTOSTART_REPLICAS = "harvui.io/autostart-replicas"

# injected by server.py so this module stays import-cycle free
kget = ksend = None
SYS_NS = set()
_cache = {}
hardware_features = lambda: []
create_pvc = None
STORAGE_CLASS = "longhorn-r2"


def dns_label(value, label):
    value = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(f"{label} must use lowercase letters, numbers and dashes")
    return value


def bind(_kget, _ksend, _sys_ns, _cache_ref, _hardware_features=None,
         _create_pvc=None, _storage_class=None):
    global kget, ksend, SYS_NS, _cache, hardware_features, create_pvc, STORAGE_CLASS
    kget, ksend, SYS_NS, _cache = _kget, _ksend, _sys_ns, _cache_ref
    if _hardware_features:
        hardware_features = _hardware_features
    if _create_pvc:
        create_pvc = _create_pvc
    if _storage_class:
        STORAGE_CLASS = _storage_class


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


def _container_feature_ids(spec, container, definitions):
    volumes = {v.get("name"): v for v in spec.get("volumes", []) or []}
    found = []
    for mount in container.get("volumeMounts", []) or []:
        volume = volumes.get(mount.get("name"), {})
        host_path = (volume.get("hostPath") or {}).get("path", "").rstrip("/")
        mount_path = str(mount.get("mountPath") or "").rstrip("/")
        for feature in definitions:
            expected_host = feature["host_path"].rstrip("/")
            expected_mount = feature["container_path"].rstrip("/")
            if host_path and (host_path == expected_host or host_path.startswith(expected_host + "/")):
                if mount_path == expected_mount and feature["id"] not in found:
                    found.append(feature["id"])
    return found


def _apply_container_edit(container, change, workload_name):
    if "name" in change:
        container["name"] = dns_label(change.get("name"), "container name")
    if "image" in change:
        image = str(change.get("image") or "").strip()
        if not image:
            raise ValueError(f"{container['name']}: image is required")
        container["image"] = image
    if "env" in change:
        refs = [copy.deepcopy(item) for item in container.get("env", []) or []
                if item.get("valueFrom") and item.get("name")]
        protected = {item["name"] for item in refs}
        literals = [{"name": str(key), "value": str(value)}
                    for key, value in (change.get("env") or {}).items()
                    if key not in protected]
        env = refs + literals
        if env:
            container["env"] = env
        else:
            container.pop("env", None)
    if "cpu" in change or "memory" in change:
        resources = container.setdefault("resources", {})
        requests = resources.setdefault("requests", {})
        for key in ("cpu", "memory"):
            if key not in change:
                continue
            value = str(change.get(key) or "").strip()
            if value:
                requests[key] = value
            else:
                requests.pop(key, None)
        if not requests:
            resources.pop("requests", None)
        if not resources:
            container.pop("resources", None)
    if "ports" in change:
        ports = []
        for item in change.get("ports") or []:
            number = int(item.get("container") or item.get("containerPort") or 0)
            if not 1 <= number <= 65535:
                raise ValueError(f"{container['name']}: container ports must be between 1 and 65535")
            protocol = str(item.get("protocol") or "TCP").upper()
            if protocol not in ("TCP", "UDP", "SCTP"):
                raise ValueError(f"{container['name']}: unsupported port protocol {protocol}")
            port = {"containerPort": number, "protocol": protocol}
            port_name = str(item.get("name") or "").strip()
            if port_name:
                port["name"] = dns_label(port_name[:15], "port name")
            ports.append(port)
        if ports:
            container["ports"] = ports
        else:
            container.pop("ports", None)


def _unique_volume_name(base, used):
    base = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:55] or "volume"
    candidate, suffix = base, 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = base[:63 - len(tail)].rstrip("-") + tail
        suffix += 1
    used.add(candidate)
    return candidate


def _is_storage_volume(volume):
    """True for the volume kinds the storage picker owns."""
    return bool(volume.get("persistentVolumeClaim") or volume.get("hostPath")
                or "emptyDir" in volume)


def _requested_volume_kind(row):
    kind = str(row.get("kind") or "").strip()
    if kind:
        return kind
    kind_by_type = {"host": "host", "emptyDir": "ephemeral", "pod": "pod"}
    if row.get("type") in kind_by_type:
        return kind_by_type[row["type"]]
    if row.get("create") is False:
        return "existing"
    return "new-rwx" if row.get("access_mode") == "ReadWriteMany" else "new-rwo"


def _device_host_paths():
    return {feature["host_path"].rstrip("/") for feature in hardware_features()}


def _is_device_mount(volume, devices):
    path = (volume.get("hostPath") or {}).get("path", "").rstrip("/")
    return bool(path) and any(path == device or path.startswith(device + "/") for device in devices)


def _pvc_exists(ns, name):
    try:
        kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def _apply_container_volumes(ns, spec, container_requests):
    """Rewrite the storage a container mounts and report the claims to create.

    Only claims, host paths and emptyDir volumes are owned by the storage
    picker.  Hardware device mounts, ConfigMap and Secret volumes are
    Kubernetes wiring that the editor never repoints, so they survive untouched.
    Claims are returned rather than created here so that every container is
    validated before anything is written to the cluster.
    """
    requests = [(container, change.get("volumes") or [])
                for container, change in container_requests if "volumes" in change]
    if not requests:
        return []

    pod_volumes = spec.get("volumes", []) or []
    by_name = {volume.get("name"): volume for volume in pod_volumes}
    devices = _device_host_paths()
    used = set(by_name)
    pending, seen_claims = [], set()

    def reuse(match):
        return next((volume.get("name") for volume in pod_volumes if match(volume)), "")

    for container, rows in requests:
        name = container.get("name", "container")
        kept = []
        for mount in container.get("volumeMounts", []) or []:
            volume = by_name.get(mount.get("name"), {})
            if not _is_storage_volume(volume) or _is_device_mount(volume, devices):
                kept.append(mount)
        mounts, paths = [], {str(mount.get("mountPath") or "").rstrip("/") for mount in kept}
        for index, row in enumerate(rows):
            path = str(row.get("path") or "").strip()
            if not path.startswith("/"):
                raise ValueError(f"{name}: storage mount path must be absolute, got "
                                 f"{path or '(blank)'}")
            if path.rstrip("/") in paths:
                raise ValueError(f"{name}: {path} is mounted twice")
            paths.add(path.rstrip("/"))
            kind = _requested_volume_kind(row)
            source = str(row.get("source") or "").strip()
            requested_name = str(row.get("volume_name") or "").strip()
            if kind == "pod":
                if source not in by_name or not _is_storage_volume(by_name[source]):
                    raise ValueError(f"{name}: pod volume {source or '(blank)'} does not exist "
                                     "in this workload")
                volume_name = source
            elif kind == "ephemeral":
                volume_name = requested_name if "emptyDir" in by_name.get(requested_name, {}) else ""
                if not volume_name:
                    volume_name = _unique_volume_name(f"hs-{name}-{index + 1}", used)
                    pod_volumes.append({"name": volume_name, "emptyDir": {}})
            elif kind == "host":
                if not source:
                    raise ValueError(f"{name}: {path} needs a host path")
                volume_name = reuse(lambda volume: (volume.get("hostPath") or {}).get("path") == source
                                    and not _is_device_mount(volume, devices))
                if not volume_name:
                    volume_name = _unique_volume_name(f"hs-{name}-{index + 1}", used)
                    pod_volumes.append({"name": volume_name, "hostPath": {"path": source}})
            else:
                claim = dns_label(source, "volume name")
                volume_name = reuse(lambda volume: (volume.get("persistentVolumeClaim") or {})
                                    .get("claimName") == claim)
                if not volume_name:
                    volume_name = _unique_volume_name(f"hs-{name}-{index + 1}", used)
                    pod_volumes.append({"name": volume_name,
                                        "persistentVolumeClaim": {"claimName": claim}})
                if kind in ("new-rwo", "new-rwx") and claim not in seen_claims:
                    seen_claims.add(claim)
                    if _pvc_exists(ns, claim):
                        raise ValueError(f"volume {claim} already exists — choose "
                                         "“Existing PVC” to mount it without recreating it")
                    pending.append({
                        "name": claim,
                        "size_gb": max(1, int(row.get("size_gb") or 5)),
                        "storage_class": str(row.get("storage_class") or "").strip() or STORAGE_CLASS,
                        "access_mode": "ReadWriteMany" if kind == "new-rwx" else "ReadWriteOnce",
                    })
            mount = {"name": volume_name, "mountPath": path}
            if row.get("read_only"):
                mount["readOnly"] = True
            mounts.append(mount)
        merged = kept + mounts
        if merged:
            container["volumeMounts"] = merged
        else:
            container.pop("volumeMounts", None)

    # Drop storage volumes nothing mounts any more, leaving Kubernetes wiring alone.
    mounted = {mount.get("name")
               for group in ("containers", "initContainers")
               for item in spec.get(group, []) or []
               for mount in item.get("volumeMounts", []) or []}
    remaining = [volume for volume in pod_volumes
                 if volume.get("name") in mounted or not _is_storage_volume(volume)
                 or _is_device_mount(volume, devices)]
    if remaining:
        spec["volumes"] = remaining
    else:
        spec.pop("volumes", None)
    return pending


def _create_pending_pvcs(ns, pending):
    if not pending:
        return
    if not create_pvc:
        raise ValueError("creating volumes is unavailable on this server")
    for claim in pending:
        create_pvc(ns, claim["name"], claim["size_gb"], claim["storage_class"],
                   claim["access_mode"])


def _apply_container_hardware(spec, dep, requested):
    definitions = hardware_features()
    by_id = {feature["id"]: feature for feature in definitions}
    devices = {}
    for feature in definitions:
        slug = feature["id"].replace("_", "-")[:50].strip("-")
        volume_name = f"hw-{slug}-{hashlib.sha1(feature['id'].encode()).hexdigest()[:6]}"
        devices[feature["id"]] = (feature["label"], volume_name, feature["host_path"],
                                   feature["container_path"], feature["path_type"])

    desired = {id(container): set(_container_feature_ids(spec, container, definitions))
               for container in spec.get("containers", [])}
    for container, feature_ids in requested:
        wanted = set(feature_ids or [])
        unknown = wanted - set(by_id)
        if unknown:
            raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
        desired[id(container)] = wanted

    managed_names = {entry[1] for entry in devices.values()} | {"dri", "coral", "coral-usb"}
    for container in spec.get("containers", []):
        mounts = [mount for mount in container.get("volumeMounts", []) or []
                  if mount.get("name") not in managed_names]
        if mounts:
            container["volumeMounts"] = mounts
        else:
            container.pop("volumeMounts", None)
    volumes = [volume for volume in spec.get("volumes", []) or []
               if volume.get("name") not in managed_names]

    union = set()
    added_volumes = set()
    for container in spec.get("containers", []):
        for feature_id in sorted(desired.get(id(container), set())):
            union.add(feature_id)
            label, volume_name, host_path, container_path, path_type = devices[feature_id]
            if volume_name not in added_volumes:
                volumes.append({"name": volume_name,
                                "hostPath": {"path": host_path, "type": path_type}})
                added_volumes.add(volume_name)
            container.setdefault("volumeMounts", []).append(
                {"name": volume_name, "mountPath": container_path})
            container.setdefault("securityContext", {})["privileged"] = True
    if volumes:
        spec["volumes"] = volumes
    else:
        spec.pop("volumes", None)

    selectors = spec.setdefault("nodeSelector", {})
    for feature in definitions:
        selectors.pop(feature["label"], None)
    for feature_id in union:
        selectors[devices[feature_id][0]] = "true"
    if not selectors:
        spec.pop("nodeSelector", None)
    annotations = dep["metadata"].setdefault("annotations", {})
    if union:
        annotations["harvui.io/hardware"] = ",".join(sorted(union))
    else:
        annotations.pop("harvui.io/hardware", None)


def edit_workload(cfg):
    """Edit pod settings and one or more containers in an existing Deployment."""
    ns, name = cfg["ns"], cfg["name"]
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    spec = dep["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    if not containers:
        raise ValueError(f"{name} has no editable containers")

    container_requests = []
    if "containers" in cfg:
        existing = {container.get("name"): container for container in containers}
        seen = set()
        for change in cfg.get("containers") or []:
            original = str(change.get("original_name") or change.get("name") or "")
            if original not in existing:
                raise ValueError(f"container {original or '(unnamed)'} no longer exists in {name}")
            if original in seen:
                raise ValueError(f"container {original} was submitted more than once")
            seen.add(original)
            container_requests.append((existing[original], change))
        final_names = []
        requested_by_original = {str(change.get("original_name") or change.get("name")): change
                                 for _, change in container_requests}
        for container in containers:
            change = requested_by_original.get(container.get("name"), {})
            final_names.append(dns_label(change.get("name") or container.get("name"), "container name"))
        if len(final_names) != len(set(final_names)):
            raise ValueError(f"container names must be unique in {name}")
        for container, change in container_requests:
            _apply_container_edit(container, change, name)
    else:
        # Backward-compatible single-container request used by older clients.
        legacy = {key: cfg[key] for key in ("image", "env", "cpu", "memory", "ports") if key in cfg}
        if "container_name" in cfg:
            legacy["name"] = cfg["container_name"]
        if legacy:
            final_name = dns_label(legacy.get("name") or containers[0].get("name"), "container name")
            if any(other is not containers[0] and other.get("name") == final_name for other in containers):
                raise ValueError(f"container {final_name} already exists in {name}")
            _apply_container_edit(containers[0], legacy, name)
    pending_claims = _apply_container_volumes(ns, spec, container_requests)
    if "pod_hostname" in cfg:
        pod_hostname = (cfg.get("pod_hostname") or "").strip()
        if pod_hostname:
            spec["hostname"] = dns_label(pod_hostname, "pod hostname")
        else:
            spec.pop("hostname", None)

    if "seed_configs" in cfg:
        _save_seed_configs(ns, dep, cfg.get("seed_configs") or [])

    if "replicas" in cfg or "autostart" in cfg:
        requested = int(cfg.get("replicas", dep["spec"].get("replicas", 1)) or 0)
        autostart = bool(cfg["autostart"]) if "autostart" in cfg else requested > 0
        annotations = dep["metadata"].setdefault("annotations", {})
        # Kubernetes has no boot-time start: a workload runs exactly when its
        # replica count is above zero. Autostart off therefore scales to zero
        # and parks the wanted count so turning it back on restores it.
        if autostart:
            dep["spec"]["replicas"] = max(1, requested)
            annotations.pop(AUTOSTART_REPLICAS, None)
        else:
            dep["spec"]["replicas"] = 0
            annotations[AUTOSTART_REPLICAS] = str(max(1, requested))
    hardware_requests = [(container, change.get("hardware") or [])
                         for container, change in container_requests if "hardware" in change]
    if hardware_requests:
        _apply_container_hardware(spec, dep, hardware_requests)
    elif "hardware" in cfg or "gpu" in cfg:
        wanted = set(cfg.get("hardware") or [])
        if cfg.get("gpu"):
            wanted.add("igpu")
        _apply_container_hardware(spec, dep, [(containers[0], wanted)])
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
    # Every container is validated by now, so new claims can be created safely.
    _create_pending_pvcs(ns, pending_claims)
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
