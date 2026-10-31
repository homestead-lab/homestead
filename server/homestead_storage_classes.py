"""Storage classes: making, defaulting and removing them, the V2 engine they
may ask for, and the restore classes Homestead makes and tidies.

server.py keeps storage_classes() itself, which also tracks the class new
volumes go on when none is chosen; it is bound here to read them.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse

import homestead_components as COMPONENTS
import homestead_disks as DISKS
import homestead_cache as CACHE
import homestead_names as NAMES
import homestead_platform as PLATFORM
import homestead_shared as SHARED
import homestead_storage_resize as STORAGE_RESIZE

kget = ksend = storage_classes = _dns_name = node_temps = None
selectable = shared = facts = None
_cache = {}
DATA_DIR = "/data"
DEFAULT_NS = "lab"


def bind(_kget, _ksend, _storage_classes, dns_name, _node_temps, cache, data_dir, default_namespace,
         _selectable=None, _shared=None, _facts=None):
    """_selectable, _shared and _facts are server.py's class pickers: they put
    the class new volumes go on first, which server.py keeps."""
    global kget, ksend, storage_classes, _dns_name, node_temps, _cache, DATA_DIR, DEFAULT_NS
    global selectable, shared, facts
    kget, ksend, storage_classes, _dns_name, node_temps = _kget, _ksend, _storage_classes, dns_name, _node_temps
    _cache, DATA_DIR, DEFAULT_NS = cache, data_dir, default_namespace
    selectable, shared, facts = _selectable, _shared, _facts


def cleanup_restore_classes():
    """Remove unused restore classes, retaining those needed for PVC expansion.

    Bound claims still need their class for resizing. Restore classes are
    excluded from regular pickers separately, so retaining them adds no clutter.
    """
    try:
        claims = kget("/api/v1/persistentvolumeclaims")
        if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
            return []
        volumes = kget("/api/v1/persistentvolumes")
        classes = kget("/apis/storage.k8s.io/v1/storageclasses")
        if any("items" not in collection or (collection.get("metadata") or {}).get("continue")
               for collection in (volumes, classes)):
            return []
        in_use = {STORAGE_RESIZE.storage_class(p) for p in claims["items"]}
        in_use.update((p.get("spec") or {}).get("storageClassName") for p in volumes["items"])
        items = classes["items"]
    except Exception:
        return []
    removed = []
    for item in items:
        name = item["metadata"]["name"]
        if name.startswith("homestead-restore-") and name not in in_use:
            try:
                ksend("DELETE", f"/apis/storage.k8s.io/v1/storageclasses/{name}")
                removed.append(name)
            except Exception:
                pass
    CACHE.drop_where(_cache, lambda key: key.startswith(("stor", "sc")))
    return removed


DEFAULT_CLASS_ANNOTATION = "storageclass.kubernetes.io/is-default-class"
LONGHORN_PROVISIONER = "driver.longhorn.io"


def storage_class_usage():
    """How many claims each class is backing, so deletion can be guarded."""
    counts = {}
    claims = kget("/api/v1/persistentvolumeclaims")
    if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
        raise ValueError("Could not check every claim using the storage classes; try again")
    for item in claims["items"]:
        name = STORAGE_RESIZE.storage_class(item)
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def storage_class_inventory():
    usage = storage_class_usage()
    rows = storage_classes()
    for row in rows:
        row["in_use"] = usage.get(row["name"], 0)
    return rows


def create_storage_class(cfg):
    """Create a Longhorn StorageClass.

    Kubernetes treats a StorageClass as immutable apart from its default flag
    and expansion setting, so Homestead offers create and delete rather than an
    edit that would silently do nothing.
    """
    name = _dns_name(cfg.get("name"), "storage class name")
    if any(row["name"] == name for row in storage_classes()):
        raise ValueError(f"storage class {name} already exists")
    replicas = int(cfg.get("replicas", 2) or 2)
    if not 1 <= replicas <= 5:
        raise ValueError("replica count must be between 1 and 5")
    stale = int(cfg.get("stale_replica_timeout", 30) or 30)
    if not 1 <= stale <= 2880:
        raise ValueError("stale replica timeout must be between 1 and 2880 minutes")
    # Kept unless asked otherwise: deleting an app should not take its data.
    reclaim = str(cfg.get("reclaim_policy") or "Retain")
    if reclaim not in ("Delete", "Retain"):
        raise ValueError("reclaim policy must be Delete or Retain")
    # Written explicitly, the way Harvester writes its own classes: a blank
    # parameter and an explicit false behave the same in Longhorn but do not
    # read the same to anyone comparing two classes.
    parameters = {"numberOfReplicas": str(replicas), "staleReplicaTimeout": str(stale),
                  "migratable": "true" if cfg.get("migratable") else "false",
                  "encrypted": "true" if cfg.get("encrypted") else "false"}
    engine = str(cfg.get("engine") or "v1").lower()
    if engine not in ("v1", "v2"):
        raise ValueError("the data engine is v1 or v2")
    # Where the copies go. Longhorn's own default puts each on a different
    # host, so a one-host cluster can never place a second. "disks" lets
    # copies share a host but never a disk: a failed disk is survived there,
    # a failed host is not. (Longhorn 1.6 and later read these per class.)
    copies = str(cfg.get("copies") or "hosts")
    if copies not in ("hosts", "disks"):
        raise ValueError("copies go on different hosts or different disks")
    if copies == "disks":
        parameters.update(replicaSoftAntiAffinity="enabled", replicaDiskSoftAntiAffinity="disabled")
    elif cfg.get("hosts_only"):
        # Different hosts whatever Longhorn's global soft anti-affinity says:
        # Homestead's "Longhorn, two copies" class (homestead_data_copies).
        parameters["replicaSoftAntiAffinity"] = "disabled"
    warning = ""
    disk_tags, node_tags = DISKS.clean_tags(cfg.get("disk_tags")), DISKS.clean_tags(cfg.get("node_tags"))
    if disk_tags:
        parameters["diskSelector"] = ",".join(disk_tags)
    if node_tags:
        parameters["nodeSelector"] = ",".join(node_tags)
    if disk_tags or node_tags:
        # Every replica needs a node of its own with a disk that fits; fewer
        # than that and a volume runs a copy short, or does not start at all.
        reach = DISKS.tag_reach(disk_tags, node_tags)
        wanted = " and ".join(x for x in (f"a disk tagged {', '.join(disk_tags)}" if disk_tags else "",
                                          f"the node tags {', '.join(node_tags)}" if node_tags else "") if x)
        if not reach:
            warning += f"; no node has {wanted} yet, so its volumes will not schedule until one does"
        elif len(reach) < replicas:
            warning += (f"; only {', '.join(reach)} ha{'s' if len(reach) == 1 else 've'} {wanted}, fewer than "
                        f"its {replicas} replicas, so its volumes will run degraded")
    if engine == "v2":
        parameters["dataEngine"] = "v2"
        v2 = v2_engine_status()
        if not v2["enabled"]:
            warning = "; V2 is switched off in Longhorn, so its volumes will not schedule until it is on"
        elif not v2["ready_nodes"]:
            warning = "; no node has a V2 disk and hugepages yet, so its volumes will not schedule"
    body = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": name, "labels": {NAMES.key("managed"): "true"},
                         "annotations": {DEFAULT_CLASS_ANNOTATION: "true"} if cfg.get("default") else {}},
            "provisioner": str(cfg.get("provisioner") or LONGHORN_PROVISIONER),
            "parameters": parameters,
            "allowVolumeExpansion": bool(cfg.get("expandable", True)),
            "reclaimPolicy": reclaim,
            "volumeBindingMode": "Immediate"}
    if cfg.get("default"):
        _clear_default_class(name)
    ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", body)
    return {"ok": True, "name": name, "classes": storage_class_inventory(),
            "message": (f"Storage class {name} created" +
                        (" and made the default" if cfg.get("default") else "") + warning)}


V2_HUGEPAGES_MB = 2048


def _quantity_mb(value):
    """A Kubernetes memory quantity in MiB: "2Gi", "1024Mi", "0"."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]i?)?", str(value or "0").strip())
    if not match:
        return 0
    number, unit = float(match.group(1)), match.group(2) or ""
    scale = {"": 1 / 1024**2, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024**2,
             "K": 1000 / 1024**2, "M": 1000**2 / 1024**2, "G": 1000**3 / 1024**2, "T": 1000**4 / 1024**2}[unit]
    return int(number * scale)


def v2_engine_status():
    """Whether Longhorn's V2 data engine can take volumes, node by node.

    V2 (SPDK) needs three things: the engine switched on - on Harvester by its
    own longhorn-v2-data-engine-enabled setting, which drives Longhorn's - and,
    on each node that will hold a replica, a disk handed to Longhorn as a block
    device and 2 GiB of hugepages. Without them a V2 volume never schedules,
    which is worth knowing before creating a class for it."""
    def setting(path):
        try:
            item = kget(path)
            return str(item.get("value") or item.get("default") or "").lower() == "true"
        except Exception:
            return None

    enabled = setting("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/settings/v2-data-engine")
    harvester = setting("/apis/harvesterhci.io/v1beta1/settings/longhorn-v2-data-engine-enabled")
    try:
        hugepages = {node["metadata"]["name"]: _quantity_mb(
            ((node.get("status", {}) or {}).get("allocatable", {}) or {}).get("hugepages-2Mi"))
            for node in kget("/api/v1/nodes").get("items", [])}
    except Exception:
        hugepages = {}
    nodes = []
    try:
        longhorn_nodes = kget("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes").get("items", [])
    except Exception:
        longhorn_nodes = []
    try:
        probes = node_temps()
    except Exception:
        probes = {}
    for node in longhorn_nodes:
        name = node["metadata"]["name"]
        disks = ((node.get("spec", {}) or {}).get("disks", {}) or {}).values()
        block = [disk for disk in disks if str(disk.get("diskType", "")).lower() == "block"
                 and disk.get("allowScheduling", True)]
        pages = hugepages.get(name, 0)
        facts = (probes.get(name) or {}).get("v2") or {}
        modules = facts.get("modules") or {}
        cpu = True if facts.get("arch") in ("aarch64", "arm64") else facts.get("sse4_2")
        # Each check: True, False, or None where the node probe has not said.
        checks = {"cpu": cpu,
                  "modules": all(modules.values()) if modules else None,
                  "hugepages": pages >= V2_HUGEPAGES_MB,
                  "disk": bool(block)}
        missing = ([] if block else ["a V2 (block) disk"]) + (
            [] if pages >= V2_HUGEPAGES_MB else [f"{V2_HUGEPAGES_MB // 1024} GiB of hugepages (has {pages} MiB)"]) + (
            [f"kernel modules {', '.join(m for m, ok in modules.items() if not ok)}"] if checks["modules"] is False else []) + (
            ["a CPU with SSE4.2"] if cpu is False else [])
        nodes.append({"name": name, "block_disks": len(block), "hugepages_mb": pages,
                      "ready": not missing, "missing": missing, "checks": checks,
                      "missing_modules": [m for m, ok in modules.items() if not ok]})
    ready = sum(1 for node in nodes if node["ready"])
    platform = PLATFORM.detect()
    try:
        longhorn = COMPONENTS.longhorn_version()
    except Exception:
        longhorn = ""
    return {"enabled": bool(enabled), "harvester_setting": harvester, "nodes": nodes,
            "ready_nodes": ready, "total_nodes": len(nodes),
            "distribution": platform.get("distribution", ""), "longhorn_version": longhorn,
            # V2 is Longhorn's to run from 1.8; before that it is an experiment.
            "longhorn_ok": bool(COMPONENTS.parse(longhorn)) and COMPONENTS.parse(longhorn)[:2] >= (1, 8)}


def _chosen_default_path():
    return os.path.join(DATA_DIR, "default-class.json")


def chosen_default_class():
    """The class someone made the default here, if anyone has."""
    try:
        with open(_chosen_default_path(), encoding="utf-8") as handle:
            return str((json.load(handle) or {}).get("name") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def reconcile_default_class():
    """k3s marks its local-path class as the default again every time it
    starts, and Longhorn's chart marks its own; with two defaults a claim
    that names no class lands on either. Keep one: the class chosen here,
    else a Longhorn one over local-path. Returns (kept, demoted) or None."""
    rows = storage_classes()
    defaults = [row for row in rows if row["default"]]
    if len(defaults) < 2:
        return None
    names = [row["name"] for row in defaults]
    chosen = chosen_default_class()
    keep = (chosen if chosen in names
            else next((row["name"] for row in defaults if row["provisioner"] == LONGHORN_PROVISIONER), names[0]))
    _clear_default_class(keep)
    return keep, [name for name in names if name != keep]


def _clear_default_class(keep):
    for row in storage_classes():
        if row["default"] and row["name"] != keep:
            ksend("PATCH", f"/apis/storage.k8s.io/v1/storageclasses/{row['name']}",
                  {"metadata": {"annotations": {DEFAULT_CLASS_ANNOTATION: "false"}}},
                  ctype="application/merge-patch+json")


def set_default_storage_class(name):
    name = _dns_name(name, "storage class name")
    row = next((item for item in storage_classes() if item["name"] == name), None)
    if not row:
        raise ValueError(f"storage class {name} does not exist")
    if row["internal"]:
        raise ValueError(f"{name} is reserved by Harvester and cannot be the default")
    _clear_default_class(name)
    ksend("PATCH", f"/apis/storage.k8s.io/v1/storageclasses/{name}",
          {"metadata": {"annotations": {DEFAULT_CLASS_ANNOTATION: "true"}}},
          ctype="application/merge-patch+json")
    # Kept, so a class k3s marks as default again at its next start is put
    # back. Only a convenience: the class is the default whether or not this
    # is written.
    try:
        SHARED.write_json(_chosen_default_path(), {"name": name, "at": int(time.time())})
    except OSError as error:
        print(f"storage: could not remember {name} as the chosen default: {error}", flush=True)
    return {"ok": True, "classes": storage_class_inventory(),
            "message": f"{name} is now the default storage class"}


def delete_storage_class(name):
    name = _dns_name(name, "storage class name")
    rows = storage_class_inventory()
    row = next((item for item in rows if item["name"] == name), None)
    if not row:
        raise ValueError(f"storage class {name} does not exist")
    if row["internal"]:
        raise ValueError(f"{name} is reserved by Harvester and must not be deleted")
    if row["default"]:
        raise ValueError(f"{name} is the default class; make another class the default first")
    if row["in_use"]:
        raise ValueError(f"{name} still backs {row['in_use']} claim"
                         f"{'s' if row['in_use'] != 1 else ''}; existing volumes keep working, "
                         "but the class cannot be removed while claims reference it")
    volumes = kget("/api/v1/persistentvolumes")
    if "items" not in volumes or (volumes.get("metadata") or {}).get("continue"):
        raise ValueError("Could not check every backing volume; the StorageClass was not deleted")
    if any((pv.get("spec") or {}).get("storageClassName") == name for pv in volumes["items"]):
        raise ValueError("This StorageClass still backs a persistent volume; keep it so retained data can be recovered and resized")
    ksend("DELETE", f"/apis/storage.k8s.io/v1/storageclasses/{name}")
    return {"ok": True, "classes": storage_class_inventory(),
            "message": f"Storage class {name} deleted; existing volumes are untouched"}


def _restore_class_repair(pvc):
    """Reconstruct a missing Homestead restore class from its bound CSI volumes."""
    spec, meta = pvc.get("spec") or {}, pvc.get("metadata") or {}
    name = spec.get("storageClassName") or ""
    labels = meta.get("labels") or {}
    if (not re.fullmatch(r"homestead-restore-[0-9a-f]{16}", name)
            or labels.get("app.kubernetes.io/managed-by") != "homestead"
            or labels.get("homestead.io/restored-volume") != "true"
            or not (meta.get("annotations") or {}).get("homestead.io/restored-from-backup")):
        raise ValueError("Only missing classes from Homestead restores can be repaired here")
    claims = kget("/api/v1/persistentvolumeclaims")
    if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
        raise ValueError("Could not check every claim using this StorageClass")
    users = [c for c in claims["items"] if ((c.get("metadata") or {}).get("annotations") or {}).get(
                 "volume.beta.kubernetes.io/storage-class", (c.get("spec") or {}).get("storageClassName")) == name]
    if not meta.get("uid") or not any((c.get("metadata") or {}).get("uid") == meta["uid"] for c in users):
        raise ValueError("The claim changed; reopen the volume editor")
    parameters = None
    reclaim = None
    for claim in users:
        cs, cm = claim.get("spec") or {}, claim.get("metadata") or {}
        if (claim.get("status") or {}).get("phase") != "Bound" or cm.get("deletionTimestamp") or not cs.get("volumeName"):
            raise ValueError("Every claim using this class must be bound and not being deleted")
        pv = kget("/api/v1/persistentvolumes/" + urllib.parse.quote(cs["volumeName"], safe=""))
        ps = pv.get("spec") or {}
        ref, csi = ps.get("claimRef") or {}, ps.get("csi") or {}
        if (ps.get("storageClassName") != name or (pv.get("status") or {}).get("phase") != "Bound"
                or not cm.get("uid") or ref.get("uid") != cm["uid"]
                or ref.get("name") != cm.get("name") or ref.get("namespace") != cm.get("namespace")
                or csi.get("driver") != "driver.longhorn.io"):
            raise ValueError("The backing volume does not match the restored claim")
        attrs = {k: v for k, v in (csi.get("volumeAttributes") or {}).items()
                 if k not in ("storage.kubernetes.io/csiProvisionerIdentity", "share")}
        if not attrs.get("fromBackup") or not all(isinstance(v, str) for v in attrs.values()):
            raise ValueError("The backing volume has no usable restore parameters")
        if csi.get("fsType"):
            attrs["fsType"] = csi["fsType"]
        policy = ps.get("persistentVolumeReclaimPolicy")
        if policy not in ("Delete", "Retain") or (parameters is not None and (parameters != attrs or reclaim != policy)):
            raise ValueError("The volumes using this class have different storage settings")
        parameters, reclaim = attrs, policy
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "homestead", "homestead.io/restore-class": "true"},
                         "annotations": {"homestead.io/resize-support-repaired": "true"}},
            "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True,
            "reclaimPolicy": reclaim, "volumeBindingMode": "Immediate", "parameters": parameters}


def repair_restore_classes():
    """Put back the restore classes older releases removed while claims still
    used them. Those claims kept working, but Kubernetes will not resize a
    claim whose class is gone, and Homestead listed them as "other" volumes.
    Each class is rebuilt from its own bound volumes (_restore_class_repair),
    which refuses anything it cannot match exactly."""
    claims = kget("/api/v1/persistentvolumeclaims")
    if "items" not in claims or (claims.get("metadata") or {}).get("continue"):
        return []
    classes = kget("/apis/storage.k8s.io/v1/storageclasses")
    if "items" not in classes or (classes.get("metadata") or {}).get("continue"):
        return []
    present = {item["metadata"]["name"] for item in classes["items"]}
    repaired = []
    for pvc in claims["items"]:
        name = (pvc.get("spec") or {}).get("storageClassName") or ""
        if not name.startswith("homestead-restore-") or name in present:
            continue
        try:
            ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", _restore_class_repair(pvc))
            repaired.append(name)
            present.add(name)
        except (ValueError, urllib.error.HTTPError) as error:
            print(f"restore class {name} not repaired: {str(error)[:160]}", flush=True)
    if repaired:
        CACHE.drop_where(_cache, lambda key: key.startswith(("stor", "sc", "vol")))
    return repaired


def repair_volume_class(cfg):
    ns, name = cfg.get("namespace") or DEFAULT_NS, cfg.get("name") or ""
    pvc = kget(f"/api/v1/namespaces/{urllib.parse.quote(ns, safe='')}/persistentvolumeclaims/{urllib.parse.quote(name, safe='')}")
    class_name = (pvc.get("spec") or {}).get("storageClassName") or ""
    if cfg.get("confirm") != class_name or not class_name:
        raise ValueError("Review the missing StorageClass before restoring it")
    try:
        kget("/apis/storage.k8s.io/v1/storageclasses/" + urllib.parse.quote(class_name, safe=""))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise ValueError("The StorageClass already exists; reopen the volume editor")
    body = _restore_class_repair(pvc)
    ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", body)
    CACHE.drop_where(_cache, lambda key: key.startswith(("stor", "sc", "vol")))
    return {"ok": True, "detail": "Resize support repaired. You can now enlarge the volume."}


def class_choices(with_facts=False):
    """The classes a person may pick; with ?facts=1 also the shared ones and
    what each is, for a picker that explains them."""
    classes = storage_classes()
    if with_facts:
        return {"names": selectable(classes), "shared": shared(classes), "facts": facts(classes)}
    return selectable(classes)


def _cleanup():
    removed = cleanup_restore_classes()
    return {"ok": True, "removed": removed,
            "detail": f"removed {len(removed)} class{'es' if len(removed) != 1 else ''} left by restores"
                      if removed else "nothing to remove: every restore class is still in use"}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/storageclasses"): ("viewer", lambda request: class_choices(
        (request.query.get("facts") or [""])[0] == "1")),
    ("GET", "/api/storage/classes"): ("viewer", lambda request: storage_class_inventory()),
    ("GET", "/api/storage/v2"): ("viewer", lambda request: v2_engine_status()),
    ("POST", "/api/storage/classes"): ("admin", lambda request: create_storage_class(request.body)),
    ("POST", "/api/storage/classes/default"): ("admin", lambda request: set_default_storage_class(request.body.get("name"))),
    ("POST", "/api/storage/classes/delete"): ("admin", lambda request: delete_storage_class(request.body.get("name"))),
    ("POST", "/api/storage/classes/cleanup"): ("admin", lambda request: _cleanup()),
    ("POST", "/api/volumes/repair-class"): ("admin", lambda request: repair_volume_class(request.body)),
}
