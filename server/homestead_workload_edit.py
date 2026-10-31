"""
Editing a workload: what an edit would change and whether it restarts the app,
applying it (containers, volumes, hardware, tier, seed configs), and renaming.

Split by feature from homestead_lifecycle (#623), moved unchanged. Names that
server.py binds there are read through it (L.name), so a replacement there
still applies; read and replace the names defined here, here.
"""
import hashlib
import copy
import re
import time
import homestead_uptime as UPTIME
import urllib.error
import homestead_names as NAMES
import homestead_env_secrets as ENVSEC
import homestead_restructure as RESTRUCTURE
import homestead_affinity as AFFINITY
import homestead_failover as FAILOVER
import homestead_memory as MEMORY
import homestead_place as PLACE
import homestead_priority as PRIORITY
import homestead_group_tiers as GROUPS
import homestead_start_order as START_ORDER


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
            cm = L.kget(f"/api/v1/namespaces/{ns}/configmaps/{cm_name}")
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


def _prepare_seed_configs(ns, dep, requested):
    """Validate and copy wired ConfigMaps without changing the cluster."""
    if not requested:
        return []
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
    prepared = []
    for cm_name, values in updates.items():
        cm = copy.deepcopy(L.kget(f"/api/v1/namespaces/{ns}/configmaps/{cm_name}"))
        data = cm.setdefault("data", {})
        for key, value in values.items():
            if key not in data:
                raise ValueError("seed config key no longer exists")
            data[key] = value
        prepared.append((f"/api/v1/namespaces/{ns}/configmaps/{cm_name}", cm))
    return prepared


# --------------------------------------------------------------- edit / rename
def _wait_for_replicas(ns, name, desired, timeout=120):
    """Wait for a Deployment to reach an unambiguous ready or stopped state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dep = L.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
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
    state = "stop" if desired == 0 else f"reach {desired}/{desired} ready instances"
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
    # Keep existing service selectors working. Add a distinct target selector
    # without rewriting workload labels already used by Services/policies.
    # A later Service may use this selector too. Each rename adds a fresh key
    # instead of overwriting the previous rename's label and breaking it.
    marker = "homestead.io/rename-" + hashlib.sha256(
        (str(dep.get("metadata", {}).get("uid", "")) + ":" + new_name).encode()).hexdigest()[:16]
    selector[marker] = new_name
    labels[marker] = new_name
    spec["replicas"] = 0
    return cloned


def rename_workload(*args, **kwargs):
    raise ValueError("Use the reviewed rename workflow; legacy rename cannot safely change workloads")



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


def _apply_container_edit(container, change, workload_name, own_secret=""):
    if "name" in change:
        container["name"] = L.dns_label(change.get("name"), "container name")
    if "image" in change:
        image = str(change.get("image") or "").strip()
        if not image:
            raise ValueError(f"{container['name']}: image is required")
        if image != container.get("image"):
            # A changed image says its tag; an unchanged one is left exactly as
            # it is, so an edit elsewhere does not roll the workload.
            import homestead_update_registry as REGISTRY
            container["image"] = REGISTRY.with_tag(image)
    if "env" in change:
        # References to the workload's own Secret came to the form as plain
        # values and return in change["env"]; other references are kept.
        refs = [copy.deepcopy(item) for item in container.get("env", []) or []
                if item.get("valueFrom") and item.get("name")
                and not (own_secret and ((item["valueFrom"].get("secretKeyRef") or {}).get("name") == own_secret))]
        protected = {item["name"] for item in refs}
        # A value masked in the form (ENVSEC.MASK: shown to someone who may
        # not see it) and sent back unchanged keeps what the Secret holds.
        held = {item["name"]: item for item in container.get("env", []) or []
                if own_secret and item.get("name")
                and ((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") == own_secret}
        literals = []
        for key, value in (change.get("env") or {}).items():
            if key in protected:
                continue
            if str(value) == ENVSEC.MASK:
                if key not in held:
                    raise ValueError(f"{container.get('name')}: {key}'s value is hidden; type it in again")
                literals.append(copy.deepcopy(held[key]))
            else:
                literals.append({"name": str(key), "value": str(value)})
        env = refs + literals
        if env:
            container["env"] = env
        else:
            container.pop("env", None)
    if "cpu" in change or "memory" in change or "memory_limit" in change:
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
        if "memory_limit" in change:
            maximum = str(change.get("memory_limit") or "").strip()
            limits = resources.setdefault("limits", {})
            if maximum:
                limits["memory"] = maximum
            else:
                limits.pop("memory", None)
            if not limits:
                resources.pop("limits", None)
        MEMORY.validate(requests.get("memory"), (resources.get("limits") or {}).get("memory"), container["name"])
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
                port["name"] = L.dns_label(port_name[:15], "port name")
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
    if row.get("type") == "emptyDir":
        return "memory" if str(row.get("medium") or "").lower() == "memory" else "ephemeral"
    kind_by_type = {"host": "host", "pod": "pod"}
    if row.get("type") in kind_by_type:
        return kind_by_type[row["type"]]
    if row.get("create") is False:
        return "existing"
    return "new-rwx" if row.get("access_mode") == "ReadWriteMany" else "new-rwo"


def _device_host_paths():
    return {feature["host_path"].rstrip("/") for feature in L.hardware_features()}


def _is_device_mount(volume, devices):
    path = (volume.get("hostPath") or {}).get("path", "").rstrip("/")
    return bool(path) and any(path == device or path.startswith(device + "/") for device in devices)


def _pvc_exists(ns, name):
    try:
        L.kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
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
            elif kind in ("ephemeral", "memory", "shm"):
                # A RAM disk, a scratch directory and an enlarged /dev/shm are
                # one object to Kubernetes, told apart only by their medium.
                empty = {}
                if kind in ("memory", "shm"):
                    empty["medium"] = "Memory"
                    if row.get("size_limit"):
                        empty["sizeLimit"] = str(row["size_limit"])
                existing = by_name.get(requested_name, {})
                matches = ("emptyDir" in existing and
                           (existing.get("emptyDir") or {}).get("medium", "") == empty.get("medium", ""))
                volume_name = requested_name if matches else ""
                if volume_name:
                    existing["emptyDir"] = empty
                else:
                    volume_name = _unique_volume_name(f"hs-{name}-{index + 1}", used)
                    pod_volumes.append({"name": volume_name, "emptyDir": empty})
            elif kind == "host":
                if not source:
                    raise ValueError(f"{name}: {path} needs a host path")
                volume_name = reuse(lambda volume: (volume.get("hostPath") or {}).get("path") == source
                                    and not _is_device_mount(volume, devices))
                if not volume_name:
                    volume_name = _unique_volume_name(f"hs-{name}-{index + 1}", used)
                    pod_volumes.append({"name": volume_name, "hostPath": {"path": source}})
            else:
                claim = L.dns_label(source, "volume name")
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
                        "storage_class": str(row.get("storage_class") or "").strip() or L.STORAGE_CLASS,
                        "access_mode": "ReadWriteMany" if kind == "new-rwx" else "ReadWriteOnce",
                    })
            mount = {"name": volume_name, "mountPath": path}
            # A folder within the volume: imports and App Store installs put
            # several of an app's paths in one volume this way. Dropping it
            # would mount the whole volume at each path.
            sub_path = str(row.get("sub_path") or "").strip().strip("/")
            if sub_path and kind not in ("ephemeral", "memory", "shm"):
                if ".." in sub_path.split("/"):
                    raise ValueError(f"{name}: the folder for {path} cannot leave its volume")
                mount["subPath"] = sub_path
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
    if not L.create_pvc:
        raise ValueError("creating volumes is unavailable on this server")
    for claim in pending:
        L.create_pvc(ns, claim["name"], claim["size_gb"], claim["storage_class"],
                   claim["access_mode"])


def _apply_container_hardware(spec, dep, requested):
    definitions = L.hardware_features()
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
        annotations[NAMES.key("hardware")] = ",".join(sorted(union))
    else:
        annotations.pop(NAMES.key("hardware"), None)


def apply_placement(dep, cfg):
    """Which apps it runs beside or apart from, how its copies spread, and its
    preferred host: one path for Edit and Deploy (#844), so both write the
    same affinity and refuse the same rules."""
    if "placement" in cfg:
        AFFINITY.apply(dep, cfg.get("placement") or {})
    if "node" in cfg:
        PLACE.apply_node_placement(dep, cfg["node"], pin=False)


def _pod_template(dep):
    """A Deployment's pod template without Homestead's own edit stamp: what,
    if it changes, makes Kubernetes replace the pods."""
    template = copy.deepcopy((dep.get("spec") or {}).get("template") or {})
    (template.get("metadata") or {}).get("annotations", {}).pop(NAMES.key("editedAt"), None)
    return template


def restarts(current, proposed):
    """Whether saving proposed over current replaces the workload's pods."""
    return _pod_template(current) != _pod_template(proposed)


def _apply_priority(spec, tier):
    """The tier chosen in Edit, written as the class the cluster's ladder uses
    for it (#429). Normal is no class at all; Kubernetes' own class, or one
    that isn't on the cluster, is left as it is, since Edit does not offer it."""
    if tier not in PRIORITY.TIERS:
        raise ValueError("the priority tier must be critical, normal or low")
    lad = PRIORITY.ladder()
    current, _ = PRIORITY.tier_of(spec, lad)
    if current in ("system", "missing", "unknown") or current == tier:
        return
    cls = PRIORITY.class_for(tier, lad)
    if cls is None:
        spec.pop("priorityClassName", None)
    else:
        spec["priorityClassName"] = cls
    spec.pop("priority", None)
    spec.pop("preemptionPolicy", None)


def _apply_tier(dep, choice):
    """Edit's tier (#430). "group": the app follows its group - the marker,
    and the class its group's tier gives, recorded as the one Homestead
    wrote. A tier: the app's own, so the marker goes. Kubernetes' own class,
    or one that isn't on the cluster, is left as it is, marker and all."""
    spec = dep["spec"]["template"]["spec"]
    annotations = dep["metadata"].setdefault("annotations", {})
    if choice != "group":
        _apply_priority(spec, choice)
        annotations.pop(GROUPS.FOLLOW, None)
        annotations.pop(GROUPS.WROTE, None)
        return
    try:
        tier = GROUPS.edit_tier(dep, choice, L.kget)
    except ValueError:
        # An app already following keeps doing so while the tiers can't be
        # read: its other changes still save, and nothing about its tier moves.
        if GROUPS.follows(dep) and GROUPS.group_of(dep) not in ("", GROUPS.OWN_GROUP) and GROUPS.stored(L.kget) is None:
            return
        raise
    if PRIORITY.tier_of(spec, PRIORITY.ladder())[0] in ("system", "missing", "unknown"):
        return
    _apply_priority(spec, tier)
    annotations[GROUPS.FOLLOW] = "group"
    if spec.get("priorityClassName"):
        annotations[GROUPS.WROTE] = spec["priorityClassName"]
    else:
        annotations.pop(GROUPS.WROTE, None)


def prepare_edit(cfg, current=None):
    """Read-only edit construction; all validation precedes side effects."""
    ns, name = cfg["ns"], cfg["name"]
    dep = copy.deepcopy(current if current is not None else L.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"))
    # What the pods were made from: an edit that leaves it alone (a logo, a
    # group, how it is monitored) changes the Deployment, never its pods.
    template_before = _pod_template(dep)
    spec = dep["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    if not containers:
        raise ValueError(f"{name} has no editable containers")

    if "priority" in cfg:
        _apply_tier(dep, cfg["priority"])
    if "starts_after" in cfg:
        # The start gate (#746): first in initContainers, rendered from the setting.
        START_ORDER.apply(dep, cfg["starts_after"], *START_ORDER.lists(L.kget))
    removed = cfg.get("remove_containers") or []
    if not isinstance(removed, list) or any(not isinstance(item, str) for item in removed):
        raise ValueError("removed containers must be a list of names")
    existing = {container.get("name"): container for container in containers}
    if len(removed) != len(set(removed)) or set(removed) - set(existing):
        raise ValueError("a removed container no longer exists or was submitted twice; reopen Edit")
    containers[:] = [container for container in containers if container.get("name") not in removed]
    container_requests = []
    if "containers" in cfg:
        changes = cfg.get("containers")
        if not isinstance(changes, list) or any(not isinstance(change, dict) for change in changes):
            raise ValueError("containers must be a list of definitions")
        seen = set()
        for change in changes:
            if change.get("new") is True:
                if change.get("original_name"):
                    raise ValueError("a new container cannot replace an existing container")
                container = {"name": L.dns_label(change.get("name"), "container name"),
                             "imagePullPolicy": "IfNotPresent"}
                if not str(change.get("image") or "").strip():
                    raise ValueError(f"{container['name']}: image is required")
                containers.append(container)
            else:
                original = str(change.get("original_name") or change.get("name") or "")
                if original not in existing or original in removed:
                    raise ValueError(f"container {original or '(unnamed)'} no longer exists in {name}")
                if original in seen:
                    raise ValueError(f"container {original} was submitted more than once")
                seen.add(original)
                container = existing[original]
            container_requests.append((container, change))
        requested = {id(container): change for container, change in container_requests}
        final_names = [L.dns_label(requested.get(id(container), {}).get("name") or container.get("name"),
                                 "container name") for container in containers]
        init_names = {container.get("name") for container in spec.get("initContainers") or []}
        if len(final_names) != len(set(final_names)) or set(final_names) & init_names:
            raise ValueError(f"container names must be unique in {name}, including init containers")
        for container, change in container_requests:
            _apply_container_edit(container, change, name, ENVSEC.own(dep))
    else:
        # Backward-compatible single-container request used by older clients.
        if not containers:
            raise ValueError("a workload must keep at least one container")
        legacy = {key: cfg[key] for key in ("image", "env", "cpu", "memory", "memory_limit", "ports") if key in cfg}
        if "container_name" in cfg:
            legacy["name"] = cfg["container_name"]
        if legacy:
            final_name = L.dns_label(legacy.get("name") or containers[0].get("name"), "container name")
            if any(other is not containers[0] and other.get("name") == final_name for other in containers):
                raise ValueError(f"container {final_name} already exists in {name}")
            _apply_container_edit(containers[0], legacy, name, ENVSEC.own(dep))
    if not containers:
        raise ValueError("a workload must keep at least one container")
    pending_claims = _apply_container_volumes(ns, spec, container_requests)
    import homestead_privileges as PRIV
    for container, change in container_requests:
        if "privileges" in change:
            # Device passthrough keeps a container privileged whatever is asked.
            hardware = bool(set(change.get("hardware") or ())) if "hardware" in change else bool(
                (container.get("securityContext") or {}).get("privileged") and any(
                    ((v.get("hostPath") or {}).get("path") or "").startswith("/dev/")
                    and (v.get("hostPath") or {}).get("path") != PRIV.TUN
                    for v in spec.get("volumes") or []))
            PRIV.apply(container, spec, change["privileges"], hardware=hardware)
    if "pod_hostname" in cfg:
        pod_hostname = (cfg.get("pod_hostname") or "").strip()
        if pod_hostname:
            spec["hostname"] = L.dns_label(pod_hostname, "pod hostname")
        else:
            spec.pop("hostname", None)
    if "service_links" in cfg:
        # Kubernetes gives every container <SERVICE>_HOST, _PORT and the like
        # for each Service in the namespace; an image that reads such a name
        # itself (IMMICH_PORT) can refuse to start. Off leaves them out; on is
        # Kubernetes' default, so an app that never set it is left as it is.
        if not isinstance(cfg["service_links"], bool):
            raise ValueError("service links are on or off")
        if not cfg["service_links"]:
            spec["enableServiceLinks"] = False
        elif spec.get("enableServiceLinks") is False:
            spec.pop("enableServiceLinks")

    seeds =_prepare_seed_configs(ns, dep, cfg.get("seed_configs") or [])

    if cfg.get("failover"):
        FAILOVER.apply(spec, cfg["failover"])

    if "replicas" in cfg or "autostart" in cfg:
        requested = int(cfg.get("replicas", dep["spec"].get("replicas", 1)) or 0)
        autostart = bool(cfg["autostart"]) if "autostart" in cfg else requested > 0
        annotations = dep["metadata"].setdefault("annotations", {})
        # Kubernetes has no boot-time start: a workload runs exactly when its
        # replica count is above zero. Autostart off therefore scales to zero
        # and parks the wanted count so turning it back on restores it.
        if autostart:
            dep["spec"]["replicas"] = max(1, requested)
            annotations.pop(L.AUTOSTART_REPLICAS, None)
        else:
            dep["spec"]["replicas"] = 0
            annotations[L.AUTOSTART_REPLICAS] = str(max(1, requested))
    hardware_requests = [(container, change.get("hardware") or [])
                         for container, change in container_requests if "hardware" in change]
    if hardware_requests or removed:
        _apply_container_hardware(spec, dep, hardware_requests)
    elif "hardware" in cfg or "gpu" in cfg:
        wanted = set(cfg.get("hardware") or [])
        if cfg.get("gpu"):
            wanted.add("igpu")
        _apply_container_hardware(spec, dep, [(containers[0], wanted)])
    if "icon" in cfg:
        ann = dep["metadata"].setdefault("annotations", {})
        if cfg["icon"]:
            ann[NAMES.key("icon")] = cfg["icon"]
            ann[NAMES.key("icon-source")] = cfg.get("icon_source", cfg["icon"])
        else:
            ann.pop(NAMES.key("icon"), None)
            ann.pop(NAMES.key("icon-source"), None)

    if "monitoring" in cfg:
        chosen = cfg.get("monitoring") or {}
        value = UPTIME.check_setting(chosen.get("mode") or "auto", chosen.get("path"),
                                     chosen.get("port") if chosen.get("mode") != "off" else None)
        ann = dep["metadata"].setdefault("annotations", {})
        if value:
            ann[NAMES.key("uptime")] = value
        else:
            ann.pop(NAMES.key("uptime"), None)
    if "placement" in cfg:
        dep["metadata"].setdefault("namespace", ns)
    apply_placement(dep, cfg)
    # Stamped only when the pods change anyway, so the stamp never causes a restart of its own.
    if _pod_template(dep) != template_before:
        dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
            NAMES.key("editedAt")] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    workload_name = L.dns_label(cfg.get("workload_name") or name, "workload name")
    return {"deployment": dep, "claims": pending_claims, "seeds": seeds, "name": workload_name}


def edit_workload(cfg, hold=False, prepared=None):
    """Commit a validated edit; hold keeps a storage restructure stopped."""
    ns, name = cfg["ns"], cfg["name"]
    prepared = prepared if prepared is not None else prepare_edit(cfg)
    dep, workload_name = prepared["deployment"], prepared["name"]
    if workload_name != name:
        raise ValueError("Use the reviewed rename workflow; save other changes separately")
    for path, cm in prepared["seeds"]:
        L.ksend("PUT", path, cm)
    _create_pending_pvcs(ns, prepared["claims"])
    held = RESTRUCTURE.hold(dep) if hold else None
    ENVSEC.externalize(ns, dep, L.kget, L.ksend, cfg.get("masked_env") or ())
    saved = L.ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    L._bust("wl", "ov", "flow", "impact:")
    return {"ok": True, "name": name, **({"held_replicas": held, "_held_receipt": saved} if hold else {})}


# homestead_lifecycle holds the bound state, so it is imported last.
import homestead_lifecycle as L  # noqa: E402
