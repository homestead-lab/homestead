"""
The image cache: the images each node holds, which of them something still
needs, the scans that list every image on a node, pre-pulling and clean-up.

Split by feature from homestead_imports (#623), moved unchanged. Names that
homestead_imports owns or that server.py binds there are read through it (I.name),
so a replacement there still applies; read and replace the names defined here, here.
"""
import json
import hashlib
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import homestead_names as NAMES
import homestead_shared as SHARED
import homestead_runtime as RUNTIME


# --------------------------------------------------------------- image cache
def _canonical_image(ref):
    ref = re.sub(r"^(?:docker-pullable|docker)://", "", str(ref or "").strip())
    if not ref:
        return ""
    name = ref.split("@", 1)[0]
    slash, colon = name.rfind("/"), name.rfind(":")
    suffix = ref[len(name):] if "@" in ref else (name[colon:] if colon > slash else ":latest")
    if colon > slash and "@" not in ref:
        name = name[:colon]
    first = name.split("/", 1)[0]
    if not ("." in first or ":" in first or first == "localhost"):
        name = "docker.io/" + (("library/" + name) if "/" not in name else name)
    return name + suffix


def _image_digest(ref):
    match = re.search(r"sha256:[0-9a-f]{64}", str(ref or ""), re.I)
    return match.group(0).lower() if match else ""


# Why an image is kept, in words for a person: the short label on the Image
# Cache page and a sentence for its tooltip. A row's "why" picks one; its
# "reason" stays the coarse word (active, rollback, stopped, scheduled,
# unreadable) that clean-up and its messages already use.
WHY = {
    "running": ("running", "A running container uses it."),
    "rollback": ("roll back", "Kept so Roll back can return to it."),
    "job-paused": ("Job paused", "A suspended Job starts from it when it is resumed."),
    "job-waiting": ("Job waiting", "A Job that has no pod running yet, or is between retries, starts from it."),
    "scheduled-job": ("scheduled job", "A scheduled job runs it the next time it is due."),
    "scaled-to-zero": ("scaled to zero", "The app is scaled to zero and starts from it."),
    "no-pods": ("no pods yet", "It wants pods that Kubernetes does not show yet, so they would start from it."),
    "vm-stopped": ("stopped VM", "A stopped virtual machine boots from it."),
    "vm-pool": ("VM pool at zero", "A virtual machine pool with no machines starts new ones from it."),
    "vm-template": ("VM template", "A VM template starts new machines from it."),
    "vm-snapshot": ("VM snapshot", "A VM snapshot restores a machine that boots from it."),
    "disk-import": ("disk import", "A disk import pulls it through the host's container runtime."),
    "scheduled-import": ("scheduled import", "A scheduled disk import pulls it through the host's container runtime."),
    "image-stream": ("image stream not read", "This import takes its image from an OpenShift image stream, which "
                     "Homestead does not read, so every image is kept until the import is gone."),
    "unreadable": ("not readable", "A record that says which images are in use could not be read, "
                   "so every image is kept until it can be."),
}


def _system_image(ref):
    return bool(re.search(
        r"(^|/)(rancher|harvester|longhornio|kubevirt|cdi-|cilium|kube-|metrics-server|"
        r"registry\.k8s\.io|pause|traefik|fleet|system-upgrade|k8snetworkplumbingwg|"
        r"multus|whereabouts|kubeovn|calico|canal|flannel|coredns|etcd|rke2|neuvector|"
        r"suse/sles/)", str(ref or ""), re.I))


def _needed_list(path, optional_group=None, forbidden_none=False):
    """Every row of one list the image cache asks "still needed?" of, or an
    error: a list that failed or was cut short must not read as nothing
    needing an image, or clean-up would remove one a workload still runs on.
    optional_group names an API group that may not be installed (KubeVirt):
    a 404 is then no rows, but only once API discovery says the group is absent.
    forbidden_none: a 403 returns None, for a list an older role may not grant;
    the caller then keeps every image (_unreadable) rather than failing."""
    try:
        listing = I.kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 403 and forbidden_none:
            error.close()
            return None
        if error.code == 404 and optional_group and _group_absent(optional_group):
            error.close()
            return []
        raise ValueError(_unread_message(path, error)) from error
    except Exception as error:
        raise ValueError(_unread_message(path, error)) from error
    if (not isinstance(listing, dict) or not isinstance(listing.get("items"), list)
            or (listing.get("metadata") or {}).get("continue")):
        raise ValueError(f"Kubernetes returned an incomplete list for {path}, so Homestead cannot tell which images "
                         "are still needed, and no image is cleaned up until it can. Try again in a moment.")
    # A row that is not an object, or whose metadata, spec or status is not
    # one, cannot say what it needs: the list as a whole is unread.
    if not all(isinstance(row, dict) and all(isinstance(row.get(key), (dict, type(None)))
                                             for key in ("metadata", "spec", "status"))
               for row in listing["items"]):
        raise ValueError(f"Kubernetes returned a list for {path} that Homestead cannot read, so it cannot tell which "
                         "images are still needed, and no image is cleaned up until it can. Try again in a moment.")
    return listing["items"]


def _group_absent(group):
    """Whether API discovery says this group is not served. Anything unclear
    reads as served, so the caller keeps its error."""
    try:
        groups = I.kget("/apis").get("groups")
    except Exception:
        return False
    return (isinstance(groups, list) and bool(groups)
            and all(isinstance(g, dict) and g.get("name") for g in groups)
            and not any(g["name"] == group for g in groups))


def image_retention_inventory():
    """Images Kubernetes still needs now or for the immediate managed rollback.
    Raises when any of its lists cannot be read in full."""
    retained = []
    pods = _needed_list("/api/v1/pods")
    for pod in pods:
        meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        if status.get("phase") in ("Succeeded", "Failed") or meta.get("deletionTimestamp"):
            continue
        statuses = {row.get("name"): row for row in
                    (status.get("initContainerStatuses", []) or []) +
                    (status.get("containerStatuses", []) or [])}
        owner = next(iter(meta.get("ownerReferences", []) or []), {})
        workload = (meta.get("labels", {}) or {}).get("app") or owner.get("name") or meta.get("name", "")
        for container in (spec.get("initContainers", []) or []) + (spec.get("containers", []) or []):
            ref = container.get("image", "")
            image_id = (statuses.get(container.get("name")) or {}).get("imageID", "")
            retained.append({
                "ref": _canonical_image(ref), "digest": _image_digest(image_id) or _image_digest(ref),
                "reason": "active", "kind": "Pod", "why": "running", "namespace": meta.get("namespace", ""),
                "workload": workload, "container": container.get("name", ""),
            })
    for dep in _needed_list("/apis/apps/v1/deployments"):
        meta = dep.get("metadata", {})
        previous = _rollback_note(meta)
        if previous is None:
            retained.append(_unreadable(meta, "update-previous", "Deployment"))
            continue
        for container, ref in previous["images"].items():
            retained.append({
                "ref": _canonical_image(ref), "digest": _image_digest(ref),
                "reason": "rollback", "kind": "Deployment", "why": "rollback", "namespace": meta.get("namespace", ""),
                "workload": meta.get("name", ""), "container": container,
                "retained_at": previous.get("at", ""),
            })
    retained += _stopped_references()
    # Deduplicate pods belonging to the same workload and digest while keeping
    # rollback and active reasons distinct.
    unique = {}
    for row in retained:
        key = (row["reason"], row.get("kind"), row["namespace"], row["workload"], row["container"],
               row["digest"] or row["ref"])
        unique[key] = row
    return [{**row, **dict(zip(("label", "detail"), WHY[row["why"]]))} for row in unique.values()]


def _stopped_references():
    """Images a workload with no pods still starts from: a Deployment or
    StatefulSet scaled to zero, a CronJob between runs, and a stopped VM that
    boots from a container image (containerDisk or kernel boot), or a VM pool
    or VM replica set scaled to zero whose template does. With no pod to
    name them they read as unused, and cleaning one up meant the next start
    - often the moment it is needed - waited on a pull.

    The same holds for a workload that wants pods but has not got them: a
    suspended Job or one between retries, a DaemonSet that matches no node,
    a Deployment that cannot make its pods (quota, a webhook). Only a status
    that shows every pod it wants is there leaves the image to its pods.

    The digest each container last ran on is used where Homestead recorded
    it; the image may be matched by its name as well, since a tag in the
    template and the digest the node lists are two names for one image.
    A template that cannot be read keeps every image (_unreadable)."""
    out = []
    template = lambda o: o["spec"]["template"]["spec"]
    for kind, path, spec_of in (
            ("Deployment", "/apis/apps/v1/deployments", template),
            ("StatefulSet", "/apis/apps/v1/statefulsets", template),
            ("DaemonSet", "/apis/apps/v1/daemonsets", template),
            ("Job", "/apis/batch/v1/jobs", template),
            ("CronJob", "/apis/batch/v1/cronjobs", lambda o: o["spec"]["jobTemplate"]["spec"]["template"]["spec"])):
        for obj in _needed_list(path):
            meta = obj.get("metadata") or {}
            waiting = _waiting(kind, obj)
            if waiting is None:
                continue          # its pods name its images, or it will not run again
            reason, why = waiting
            try:
                podspec = spec_of(obj)
            except (KeyError, TypeError):
                podspec = None
            containers = _rows(podspec, "initContainers", "containers")
            if containers is None:
                out.append(_unreadable(meta, "template", kind))
                continue
            try:
                ran = json.loads(NAMES.annotation_of(meta, "ran-digests", "{}") or "{}")
            except (TypeError, ValueError):
                ran = {}
            ran = ran if isinstance(ran, dict) else {}
            for container in containers:
                ref = container.get("image", "")
                if not ref:
                    continue
                out.append({"ref": _canonical_image(ref),
                            "digest": _image_digest(ref) or _image_digest(ran.get(container.get("name"), "")),
                            "reason": reason, "kind": kind, "why": why, "by_name": True,
                            "namespace": meta.get("namespace", ""), "workload": meta.get("name", ""),
                            "container": container.get("name", "")})
    stopped = [vm for vm in _needed_list("/apis/kubevirt.io/v1/virtualmachines", optional_group="kubevirt.io")
               if not ((vm.get("status") or {}).get("ready") is True
                       or (vm.get("status") or {}).get("printableStatus") == "Running")]
    vms = [(vm, ("spec", "template", "spec"), "VirtualMachine", "vm-stopped") for vm in stopped]
    # A VM pool or VM replica set scaled to zero has no VM or pod left to name
    # its template's disks; one with replicas has, so it is left to them.
    pools, unread = _optional_list("VirtualMachinePools", "pool.kubevirt.io", "virtualmachinepools")
    out += unread
    vms += [(row, ("spec", "virtualMachineTemplate", "spec", "template", "spec"), "VirtualMachinePool", "vm-pool")
            for row in pools if _scaled_to_zero(row.get("spec") or {})]
    sets = _needed_list("/apis/kubevirt.io/v1/virtualmachineinstancereplicasets",
                        optional_group="kubevirt.io", forbidden_none=True)
    if sets is None:      # a role from before these were named may not grant them
        out.append(_unreadable({"name": "VirtualMachineInstanceReplicaSets"}, "no permission to list", "VirtualMachineInstanceReplicaSet"))
    vms += [(row, ("spec", "template", "spec"), "VirtualMachineInstanceReplicaSet", "vm-pool") for row in sets or [] if _scaled_to_zero(row.get("spec") or {})]
    # A Harvester VM template's versions start new VMs, and a VM snapshot
    # restores one: both boot from the container disks they name.
    versions, unread = _optional_list("VirtualMachineTemplateVersions", "harvesterhci.io", "virtualmachinetemplateversions")
    out += unread
    vms += [(row, ("spec", "vm", "spec", "template", "spec"), "VirtualMachineTemplateVersion", "vm-template")
            for row in versions]
    contents, unread = _optional_list("VirtualMachineSnapshotContents", "snapshot.kubevirt.io", "virtualmachinesnapshotcontents")
    out += unread
    vms += [(row, ("spec", "source", "virtualMachine", "spec", "template", "spec"), "VirtualMachineSnapshotContent",
             "vm-snapshot") for row in contents]
    for vm, template, kind, why in vms:          # a running VM's pod names its container disks
        meta = vm.get("metadata") or {}
        disks = _disk_images(_dig(vm, template))
        if disks is None:
            out.append(_unreadable(meta, "template", kind))
            continue
        out += [_kept(meta, ref, "stopped", name, kind, why) for name, ref in disks]
    # ...and the disks a stopped VM or a template's VMs import from a registry
    # (a running VM's disks are imported already).
    for row, vmspec, kind, why in ([(vm, ("spec",), "VirtualMachine", "vm-stopped") for vm in stopped]
                                   + [(row, ("spec", "vm", "spec"), "VirtualMachineTemplateVersion", "vm-template")
                                      for row in versions]):
        templates = _rows(_dig(row, vmspec), "dataVolumeTemplates")
        if templates is None:
            out.append(_unreadable(row.get("metadata") or {}, "template", kind))
            continue
        out += _cdi_rows(row, [(_dig(t, ("metadata",)) or {}).get("name", "") or "registry"
                               for t in templates], [_dig(t, ("spec",)) for t in templates], "stopped", kind, why)
    # CDI imports that pull a registry image through the node's runtime
    # (pullMethod: node) leave it in containerd, and need it again to import.
    for plural, resource, spec_path, reason, kind, why in (
            ("DataVolumes", "datavolumes", ("spec",), "stopped", "DataVolume", "disk-import"),
            ("DataImportCrons", "dataimportcrons", ("spec", "template", "spec"), "scheduled", "DataImportCron",
             "scheduled-import")):
        rows, unread = _optional_list(plural, "cdi.kubevirt.io", resource)
        out += unread
        for row in rows:
            out += _cdi_rows(row, ["registry"], [_dig(row, spec_path)], reason, kind, why)
            if kind == "DataImportCron" and _node_pull(_dig(row, spec_path)):
                out += _imported_digests(row)
    return out


def _imported_digests(cron):
    """A DataImportCron imports its tag by digest, and a node that pulled by
    digest lists no tag to match by name: keep each digest it imported."""
    meta = cron.get("metadata") or {}
    imports = (cron.get("status") or {}).get("currentImports")
    if imports is None:
        return []
    if not isinstance(imports, list) or not all(isinstance(row, dict) for row in imports):
        return [_unreadable(meta, "source", "DataImportCron")]
    return [{"ref": "", "digest": _image_digest(row.get("digest")), "reason": "scheduled",
             "kind": "DataImportCron", "why": "scheduled-import",
             "namespace": meta.get("namespace", ""), "workload": meta.get("name", ""), "container": "registry"}
            for row in imports if _image_digest(row.get("digest"))]


def _kept(meta, ref, reason, container, kind, why):
    """A retention row for an image a record names, matched by name too."""
    return {"ref": _canonical_image(ref), "digest": _image_digest(ref), "reason": reason, "kind": kind, "why": why,
            "by_name": True,
            "namespace": meta.get("namespace", ""), "workload": meta.get("name", ""), "container": container}


def _disk_images(tspec):
    """(volume, image) for each container image a VM's template spec boots
    from (containerDisk volumes, kernel boot), or None when it cannot be read."""
    volumes = _rows(tspec, "volumes")
    domain = (tspec or {}).get("domain") or {}
    if volumes is None or not isinstance(domain, dict):
        return None
    disks = [(volume.get("name", ""), (volume.get("containerDisk") or {}).get("image", "")) for volume in volumes]
    kernel = (((domain.get("firmware") or {}).get("kernelBoot") or {}).get("container") or {})
    disks.append(("kernel-boot", kernel.get("image", "")))
    return [(name, ref) for name, ref in disks if ref]


def _cdi_rows(obj, names, specs, reason, kind, why):
    """Retention rows for the CDI import specs of one record (a DataVolume
    spec, or a DataImportCron's or VM template's DataVolume template spec):
    the image each pulls through the node, or an unreadable row."""
    meta, out = obj.get("metadata") or {}, []
    for name, spec in zip(names, specs):
        ref = _node_pull(spec)
        if ref is None:
            return [_unreadable(meta, "image stream" if _image_stream(spec) else "source", kind)]
        if ref:
            out.append(_kept(meta, ref, reason, name, kind, why))
    return out


def _image_stream(spec):
    """Whether a CDI DataVolume spec imports through the node from an OpenShift
    image stream: it names a stream, not a url, and the stream is not read
    (no permission is asked for it), so what it holds is unknown."""
    source = spec.get("source") if isinstance(spec, dict) else None
    registry = source.get("registry") if isinstance(source, dict) else None
    return (isinstance(registry, dict) and registry.get("pullMethod") == "node"
            and isinstance(registry.get("imageStream"), str) and bool(registry["imageStream"])
            and not registry.get("url"))


def _node_pull(spec):
    """The image a CDI DataVolume spec imports through the node's container
    runtime (a registry source with pullMethod node), "" when it pulls none
    that way (the importer pod's own pull, or another source), or None when
    the spec cannot be read."""
    if not isinstance(spec, dict):
        return None
    source = spec.get("source")
    if source is None:
        return ""             # sourceRef, or a blank disk
    registry = source.get("registry") if isinstance(source, dict) else None
    if not isinstance(source, dict) or not isinstance(registry, (dict, type(None))):
        return None
    if registry is None or registry.get("pullMethod") != "node":
        return ""
    url = registry.get("url")
    return url if isinstance(url, str) and url else None


def _optional_list(kind, group, resource):
    """The rows of a resource in a group that may not be installed, read at
    the version API discovery prefers that serves it (a fixed version 404s
    once the group drops it), and any retention row to add: none when
    discovery says the group or the resource is absent; an unreadable row
    when the role may not read discovery or the list."""
    try:
        path = _served_path(group, resource)
    except PermissionError:
        path = False
    if path is None:
        return [], []
    rows = None if path is False else _needed_list(path, optional_group=group, forbidden_none=True)
    if rows is None:          # a role from before these were named may not grant them
        return [], [_unreadable({"name": kind}, "no permission to list")]
    return rows, []


def _discovery(where, group=None):
    """GET one discovery document. A 404 for a group discovery says is absent
    is None; a 403 raises PermissionError; anything else that fails is a
    ValueError, so no image is called unused on a guess."""
    try:
        return I.kget(where)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 403:
            raise PermissionError(where) from error
        if error.code == 404 and group and _group_absent(group):
            return None
        raise ValueError(_unread_message(where, error)) from error
    except Exception as error:
        raise ValueError(_unread_message(where, error)) from error


def _served_path(group, resource):
    """/apis/<group>/<version>/<resource> at the first version that serves the
    resource, the preferred one first; None when discovery says the group is
    not served or serves the resource at no version. Anything unclear is an
    error, so no image is called unused on a guess."""
    unclear = ValueError(f"Kubernetes did not say which version of {group} it serves, so Homestead cannot tell "
                         "which images are still needed, and no image is cleaned up until it can. Try again in a moment.")
    answer = _discovery(f"/apis/{group}", group)
    if answer is None:
        return None
    preferred = answer.get("preferredVersion") if isinstance(answer, dict) else None
    listed = answer.get("versions") if isinstance(answer, dict) else None
    if not isinstance(preferred, dict) or not isinstance(listed, (list, type(None))):
        raise unclear
    order = [preferred] + [row for row in listed or [] if row != preferred]
    for row in order:
        version = row.get("groupVersion") if isinstance(row, dict) else None
        if not (isinstance(version, str) and version.startswith(group + "/") and version.count("/") == 1
                and version != group + "/"):
            raise unclear
        served = _discovery(f"/apis/{version}")
        names = served.get("resources") if isinstance(served, dict) else None
        if not isinstance(names, list) or not all(isinstance(r, dict) for r in names):
            raise unclear
        if any(r.get("name") == resource for r in names):
            return f"/apis/{version}/{resource}"
    return None


def _unread_message(path, error):
    return (f"Homestead could not read {path} from Kubernetes ({str(error)[:120]}), so it cannot tell "
            "which images are still needed, and no image is cleaned up until it can. Try again in a moment.")


def _rollback_note(meta):
    """The previous images Homestead keeps for a Deployment's Roll back, as
    {"images": {container: ref}, "at": ...}, or None when the note is there
    but cannot be read: it then keeps an image nobody can name."""
    try:
        previous = json.loads(NAMES.annotation_of(meta, "update-previous", "{}") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(previous, dict):
        return None
    images = previous.get("images", {})
    if not isinstance(images, dict) or not all(isinstance(ref, str) for ref in images.values()):
        return None
    return {**previous, "images": images}


def _unreadable(meta, what, kind=""):
    """A retention row for a record that could not be read. It holds every
    image (see _holds), and names the record so it can be fixed. An import
    from an OpenShift image stream is one: Homestead does not read the stream."""
    return {"ref": "", "digest": "", "reason": "unreadable", "kind": kind,
            "why": "image-stream" if what == "image stream" else "unreadable", "holds_all": True,
            "namespace": meta.get("namespace", ""), "workload": meta.get("name", ""), "container": what}


def _scaled_to_zero(spec):
    """Whether a workload's replicas say none run. Missing means one (the
    Kubernetes default); a value that is not a number cannot say any run,
    so its images are kept as a stopped one's."""
    replicas = spec.get("replicas")
    if replicas is None:
        return False
    return not (isinstance(replicas, int) and not isinstance(replicas, bool) and replicas > 0)


def _count(value):
    """A count from a status, or None when it is not a whole number."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _waiting(kind, obj):
    """Why a workload's template images are kept when no pod may name them:
    ("stopped" or "scheduled", the words for a person in WHY), or None when its
    pods name them (its status shows every pod it wants is there) or a Job has
    finished for good.
    A status that is missing, old or unclear proves nothing, so it keeps."""
    meta, spec = obj.get("metadata") or {}, obj.get("spec") or {}
    status = obj.get("status") if isinstance(obj.get("status"), dict) else {}
    if kind == "CronJob":
        return "scheduled", "scheduled-job"
    if kind == "Job":
        conditions = status.get("conditions")
        if isinstance(conditions, list) and any(
                isinstance(c, dict) and c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
                for c in conditions):
            return None       # finished: it makes no more pods
        if spec.get("suspend") is True:
            return "stopped", "job-paused"
        active = _count(status.get("active"))
        return None if active and active > 0 else ("scheduled", "job-waiting")     # between retries, or not started
    generation, observed = _count(meta.get("generation")), _count(status.get("observedGeneration"))
    if generation is not None and (observed is None or observed < generation):
        return "stopped", "no-pods"      # the status is from before the last change
    if kind == "DaemonSet":
        wanted, updated = _count(status.get("desiredNumberScheduled")), _count(status.get("updatedNumberScheduled"))
    else:
        if _scaled_to_zero(spec):
            return "stopped", "scaled-to-zero"
        wanted = _count(spec.get("replicas"))
        wanted = 1 if spec.get("replicas") is None else wanted
        updated = _count(status.get("updatedReplicas"))
    if wanted and wanted > 0 and updated is not None and updated >= wanted:
        return None
    return "stopped", "no-pods"


def _dig(obj, keys):
    """obj[k1][k2]... when every step is an object, else None."""
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj if isinstance(obj, dict) else None


def _rows(spec, *fields):
    """The objects in these list fields of a template spec, or None when the
    spec or a field is not what Kubernetes would hold (so it cannot be read)."""
    if not isinstance(spec, dict):
        return None
    rows = []
    for field in fields:
        value = spec.get(field) or []
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            return None
        rows += value
    return rows


def _holds(row, digest, aliases):
    """Whether a retention row names this cached image. A row for a record
    Homestead could not read names every image: it cannot tell which one the
    record keeps, so none is called unused until the record is fixed."""
    if row.get("holds_all"):
        return True
    if row["digest"] and row["digest"] == digest:
        return True
    return row["ref"] in aliases and (not row["digest"] or bool(row.get("by_name")))


def image_cache():
    """What container images each node already has on disk."""
    nodes = I.kget("/api/v1/nodes").get("items", [])
    retained = image_retention_inventory()
    _load_scans()
    # Reading only: the leader's tick saves scans, removes their pods and
    # starts the next one (#622).
    scanning = _collect_scans()
    per_node, totals = [], {}
    full = True
    for n in nodes:
        name = n["metadata"]["name"]
        scanned = _scanned_images(name)
        full = full and scanned is not None
        imgs = _merged(scanned, n["status"].get("images", []) or [])
        rows = []
        for i in imgs:
            names = sorted(set(i.get("names") or ["<none>"]))
            digest_name = next((ref for ref in names if _image_digest(ref)), "")
            tag = next((ref for ref in names if not _image_digest(ref)), digest_name or "<none>")
            digest = _image_digest(digest_name)
            identity = digest or _canonical_image(tag) or tag
            sz = round((i.get("sizeBytes") or 0) / 1024**2, 1)
            rows.append({"name": tag, "names": names, "digest": digest, "size_mb": sz})
            totals[identity] = totals.get(identity, {"name": tag, "names": set(),
                                                       "digest": digest, "size_mb": sz, "nodes": []})
            totals[identity]["names"].update(names)
            totals[identity]["nodes"].append(name)
        rows.sort(key=lambda x: -x["size_mb"])
        per_node.append({"node": name, "count": len(rows),
                         "total_gb": round(sum(r["size_mb"] for r in rows) / 1024, 1),
                         "images": rows, "complete": scanned is not None,
                         "scanned_at": int((_SCANS.get(name) or {}).get("at", 0))})
    shared = []
    for value in totals.values():
        aliases = {_canonical_image(ref) for ref in value["names"]}
        reasons = [row for row in retained if _holds(row, value["digest"], aliases)]
        shared.append({"name": value["name"], "names": sorted(value["names"]),
                       "digest": value["digest"], "size_mb": value["size_mb"],
                       "nodes": sorted(set(value["nodes"])),
                       "system": _system_image(value["name"]),
                       "protected": bool(reasons), "retained_by": reasons})
    shared.sort(key=lambda x: -x["size_mb"])
    # Whoever is looking at the image cache wants to know a pull is running,
    # or has finished; the leader's tick clears a finished one away.
    pulls = prepull_status(sweep=False)
    return {"nodes": per_node, "images": shared, "complete": full, "scanning": scanning,
            "pulls": pulls["pulls"], "pulls_finished": pulls["finished"],
            "distinct": len(shared),
            "node_names": [n["metadata"]["name"] for n in nodes],
            "retained": retained,
            "protected": sum(1 for image in shared if image["protected"])}


# ---- every image a node holds ----------------------------------------------
# A node's status lists only its largest images (the kubelet reports fifty by
# default), so plenty that are running never showed. A scan asks containerd on
# each node for all of them, and the answer stands in for the status list
# until the next scan.
SCAN_TASK = "image-scan"
# A scan this old is asked again. Until the new one is in, the old one is
# still used, with whatever Kubernetes lists added: images are pulled far more
# often than removed, and dropping back to the fifty largest hid most apps.
SCAN_FRESH = 15 * 60
# However stale, a scan is not started again sooner than this after the last.
SCAN_RETRY = 5 * 60
# Kept on Homestead's own volume (homestead_imports.SCAN_DIR, which the server
# sets), so another replica, or this one after a restart, has every image
# rather than only the ones Kubernetes lists.
SCAN_STORE = "image-scans.json"
_SCANS = {}          # node -> {"at": time, "images": [...]}
_SCAN_FAILED = {}    # node -> when its last scan failed; it waits as a fresh one would
_SCAN_STARTED = [0.0]
_scan_lock = SHARED.SharedLock("image-scans")


def _scan_path():
    return os.path.join(I.SCAN_DIR, SCAN_STORE) if I.SCAN_DIR else ""


def _load_scans():
    """Take in any newer scan another replica has saved."""
    path = _scan_path()
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return
    for node, entry in (stored.get("nodes") or {}).items():
        if isinstance(entry, dict) and float(entry.get("at") or 0) > float((_SCANS.get(node) or {}).get("at") or 0):
            _SCANS[node] = {"at": float(entry["at"]), "images": entry.get("images") or []}
    _SCAN_STARTED[0] = max(_SCAN_STARTED[0], float(stored.get("started") or 0))


def _save_scans():
    path = _scan_path()
    if not path:
        return
    with _scan_lock:
        try:
            _load_scans()
            SHARED.write_json(path, {"started": _SCAN_STARTED[0], "nodes": _SCANS}, separators=(",", ":"))
        except OSError:
            pass


def _merged(scanned, listed):
    """A node's images: the scan, and anything Kubernetes lists that it has
    not seen - pulled since."""
    if scanned is None:
        return listed
    seen = {name for image in scanned for name in image.get("names") or []}
    return scanned + [image for image in listed if not seen & set(image.get("names") or [])]


def tick():
    """The leader's minute (#622): what looking at the image cache or the
    imports used to do as it read. Finished scans are saved and their pods
    removed, a host whose scan is old is scanned again, finished pre-pulls are
    cleared away, and a finished copy gives back the password it borrowed."""
    _load_scans()
    if not _collect_scans(tidy=True):
        _rescan_if_stale(I.kget("/api/v1/nodes").get("items", []))
    prepull_status(sweep=True)
    CONTAINER_IMPORT._drop_finished_copies()
    I._bust("imgcache")
    # Hourly: what is unused, and automatic clean-up when it is on.
    try:
        import homestead_image_cleanup as CLEANUP
        CLEANUP.auto_tick()
    except Exception as error:
        print(f"image clean-up round not done: {str(error)[:160]}", flush=True)


def _scannable(node):
    """A node a scan pod can run on: Ready and not cordoned. Only these count
    towards a scan being due, or one host that is never scanned would have
    every host scanned again at each retry (#622)."""
    conditions = {c.get("type"): c.get("status") for c in (node.get("status") or {}).get("conditions") or []}
    return conditions.get("Ready") == "True" and not (node.get("spec") or {}).get("unschedulable")


def _rescan_if_stale(nodes):
    """Start a scan when a Ready node has none, or an old one, so the list is
    whole for whoever looks - not only an admin who presses Scan. A node whose
    last scan failed waits as long as one whose scan worked."""
    now = time.time()
    ready = [n["metadata"]["name"] for n in nodes if _scannable(n)]
    stale = [name for name in ready
             if now - max(float((_SCANS.get(name) or {}).get("at") or 0), _SCAN_FAILED.get(name, 0)) > SCAN_FRESH]
    if not stale or now - _SCAN_STARTED[0] < SCAN_RETRY:
        return []
    try:
        return start_image_scan()["nodes"]
    except Exception:
        return []


def start_image_scan():
    """One pod per Ready node, printing everything its containerd holds."""
    started = []
    attempt = format(int(time.time()), "x")[-6:]
    # Counted as started even if a pod cannot be made part way, so the next
    # tick does not start the ones that were made again.
    _SCAN_STARTED[0] = time.time()
    try:
        for node in I.kget("/api/v1/nodes").get("items", []):
            if not _scannable(node):
                continue
            name = node["metadata"]["name"]
            suffix = hashlib.sha256(name.encode()).hexdigest()[:10]
            body = RUNTIME.pod(f"homestead-image-scan-{suffix}-{attempt}", I.NS, name,
                               f"{RUNTIME.CRICTL} images -o json", SCAN_TASK,
                               {NAMES.key("cache-node"): name}, memory="64Mi", deadline=120)
            body["spec"]["automountServiceAccountToken"] = False
            I.ksend("POST", f"/api/v1/namespaces/{I.NS}/pods", body)
            started.append(name)
    finally:
        _save_scans()
        I._bust("imgcache")
    return {"ok": True, "nodes": started,
            "detail": f"asking containerd on {len(started)} node{'s' if len(started) != 1 else ''} for every image"}


def _collect_scans(tidy=False):
    """Read finished scan pods into _SCANS; say which nodes are still being
    scanned. Only the leader's tick (tidy) saves them and removes the pods: a
    viewer's read writes nothing (#622)."""
    running = []
    for pod in NAMES.find(f"/api/v1/namespaces/{I.NS}/pods", "task", SCAN_TASK):
        meta, phase = pod.get("metadata") or {}, (pod.get("status") or {}).get("phase")
        node = NAMES.annotation_of(meta, "cache-node", "") or (pod.get("spec") or {}).get("nodeName", "")
        if phase == "Succeeded":
            try:
                from homestead_shim import raw_get
                listed = json.loads(raw_get(f"/api/v1/namespaces/{I.NS}/pods/{meta['name']}/log") or "{}")
                _SCANS[node] = {"at": time.time(), "images": [
                    {key: image.get(key) for key in ("repoTags", "repoDigests", "size")}
                    for image in listed.get("images") or []]}
                if tidy:
                    _save_scans()
            except Exception:
                pass
        if phase in ("Succeeded", "Failed"):
            if not tidy:
                continue
            if phase == "Failed":
                _SCAN_FAILED[node] = time.time()
            try:
                I.ksend("DELETE", f"/api/v1/namespaces/{I.NS}/pods/{meta['name']}")
            except Exception:
                pass
        else:
            running.append(node)
    return running


def _scanned_images(node):
    """This node's images as containerd last listed them, shaped like a node
    status's; None when it has never been scanned."""
    scan = _SCANS.get(node)
    if not scan:
        return None
    return [{"names": list(i.get("repoTags") or []) + list(i.get("repoDigests") or []),
             "sizeBytes": int(i.get("size") or 0)} for i in scan["images"]]


PREPULL_NAMES = ("homestead-pull-",)


def _pullable_nodes():
    """Nodes worth warming an image onto: Ready, and not cordoned.

    A DaemonSet cannot decline the tolerations its controller adds - not-ready,
    unreachable, unschedulable and every pressure taint - so the only way to
    keep a pull off a node being drained or already falling over is to leave
    that node out of the set the pods are allowed to land on.
    """
    healthy, all_names = [], []
    for node in I.kget("/api/v1/nodes").get("items", []):
        name = (node.get("metadata", {}) or {}).get("name", "")
        if not name:
            continue
        all_names.append(name)
        conditions = (node.get("status", {}) or {}).get("conditions", []) or []
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        if ready and not (node.get("spec", {}) or {}).get("unschedulable"):
            healthy.append(name)
    return healthy, all_names


def prepull(image, nodes=None, own=False):
    """Warm an image onto every healthy node with a DaemonSet-style pull.

    own: a job's pull, under a name of its own, so two jobs pulling the same
    image never share one DaemonSet - one's cancel, the end of its pulling
    stage or its next pull would stop the other's. Without it the name is
    one per image, as before (the start gate's image, pulled once)."""
    tag = re.sub(r"[^a-z0-9-]", "-", image.split("/")[-1].split(":")[0].lower())[:30]
    name = f"homestead-pull-{tag}" + (f"-{secrets.token_hex(4)}" if own else "")
    healthy, every = _pullable_nodes()
    wanted = [node for node in (nodes or every) if node in healthy]
    skipped = sorted(set(nodes or every) - set(wanted))
    if not wanted:
        raise ValueError(f"{', '.join(skipped)} is not ready to take an image pull"
                         if skipped else "no node is ready to take an image pull")
    body = {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": name, "namespace": I.NS,
                     "labels": NAMES.labels("prepull", image=tag)},
        "spec": {"selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}},
                              "spec": {"terminationGracePeriodSeconds": 1,
                                       "initContainers": [{"name": "pull", "image": image,
                                                           "command": ["/bin/true"]}],
                                       "containers": [{"name": "pause",
                                                       "image": "registry.k8s.io/pause:3.9",
                                                       "resources": {"requests": {"cpu": "1m", "memory": "4Mi"}}}]}}},
    }
    # Always pinned, never left to the DaemonSet's own idea of every node.
    body["spec"]["template"]["spec"]["affinity"] = {"nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname",
                                   "operator": "In", "values": sorted(wanted)}]}]}}}
    for previous in (name,):
        try:
            I.ksend("DELETE", f"/apis/apps/v1/namespaces/{I.NS}/daemonsets/{previous}")
        except urllib.error.HTTPError:
            pass
    time.sleep(1)
    I.ksend("POST", f"/apis/apps/v1/namespaces/{I.NS}/daemonsets", body)
    return {"ok": True, "daemonset": name, "image": image, "nodes": sorted(wanted),
            "skipped": skipped,
            "message": f"Pulling onto {len(wanted)} node{'' if len(wanted) == 1 else 's'}"
                       + (f"; skipped {', '.join(skipped)} (cordoned or not ready)" if skipped else "")}


def prepull_status(sweep=True):
    """Every pull still running, and the finished ones cleared away.

    A DaemonSet has no notion of being done: once the init container has pulled
    the image its pod sits there holding a pause container open for good. So
    completion is read from the outside - every pod it wants is ready - and the
    DaemonSet is deleted rather than left running on every node forever.
    """
    try:
        sets = NAMES.find(f"/apis/apps/v1/namespaces/{I.NS}/daemonsets", "task", "prepull")
    except Exception:
        return {"pulls": [], "finished": []}
    pulls, finished = [], []
    for item in sets:
        name = item["metadata"]["name"]
        status = item.get("status", {}) or {}
        desired = int(status.get("desiredNumberScheduled", 0) or 0)
        ready = int(status.get("numberReady", 0) or 0)
        image = ""
        for container in (item["spec"]["template"]["spec"].get("initContainers") or []):
            image = container.get("image", "") or image
        row = {"name": name, "image": image, "desired": desired, "ready": ready,
               "complete": bool(desired) and ready >= desired}
        if row["complete"]:
            if sweep:
                stop_prepull(name)
            finished.append(row)
        else:
            pulls.append(row)
    return {"pulls": pulls, "finished": finished}


def stop_prepull(name):
    """Delete a pull DaemonSet. Deleting its pods only makes it replace them."""
    name = str(name or "")
    if not any(name.startswith(prefix) for prefix in PREPULL_NAMES) or "/" in name:
        raise ValueError("that is not an image pre-pull")
    try:
        I.ksend("DELETE", f"/apis/apps/v1/namespaces/{I.NS}/daemonsets/{name}"
                        "?propagationPolicy=Background")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    return {"ok": True, "daemonset": name, "message": f"Pre-pull {name} stopped"}


def forget_rollback(namespace, name):
    """Stop keeping a workload's previous image for a one-click rollback.

    Homestead keeps it after an update so Roll back returns to exactly it;
    forgetting it makes the old image ordinary and unused, to be cleaned up
    like any other - and Roll back is gone for that update."""
    dep = I.kget(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")
    annotations = (dep.get("metadata") or {}).get("annotations") or {}
    keys = [key for key in annotations if key.endswith("/update-previous")]
    if not keys:
        raise ValueError(f"{name} keeps no previous image")
    I.ksend("PATCH", f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
          {"metadata": {"annotations": {key: None for key in keys}}},
          ctype="application/merge-patch+json")
    I._bust("imgcache", "wl")
    return {"ok": True, "detail": f"{name} no longer keeps its previous image; it can be cleaned up now"}


def cleanup_image(digest, nodes=None, report=None):
    """Start one tightly scoped CRI removal pod per selected cache node.
    report: an image cache report just read (Clean all unused reads one for
    every image it removes), or None to read it now."""
    digest = str(digest or "").lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("cleanup requires an exact sha256 image digest")
    report = report or image_cache()
    image = next((row for row in report["images"] if row.get("digest") == digest), None)
    if not image:
        raise ValueError("that digest is not present in the node image cache")
    if image.get("protected"):
        owners = sorted({f"{row['reason']}:{row['namespace']}/{row['workload']}"
                         for row in image.get("retained_by", [])})
        raise PermissionError("image is protected by " + ", ".join(owners))
    if image.get("system"):
        raise PermissionError("Harvester and Kubernetes platform images cannot be cleaned from Homestead")
    selected = sorted(set(nodes or image["nodes"]))
    if not selected or any(node not in image["nodes"] for node in selected):
        raise ValueError("cleanup nodes must currently cache this digest")
    image_ref = next((ref for ref in image.get("names", []) if _image_digest(ref) == digest), "")
    if not image_ref:
        raise ValueError("the cache did not report a removable repository digest")
    pods = []
    crictl = RUNTIME.CRICTL
    quoted = "'" + image_ref.replace("'", "") + "'"
    # Exited containers of pods long gone still hold the image, and rmi refuses
    # while any does. Only exited ones are removed - never a running container.
    script = (f"set -e; for id in $({crictl} ps -a -q --state exited --image {quoted}); "
              f"do {crictl} rm \"$id\" >/dev/null; done; {crictl} rmi {quoted}")
    attempt = format(int(time.time()), "x")[-6:]
    for node in selected:
        suffix = hashlib.sha256((digest + "|" + node).encode()).hexdigest()[:10]
        # A name of its own per attempt: a retry used to delete the last pod and
        # recreate it at once, and Kubernetes had not finished deleting it.
        pod_name = f"homestead-image-clean-{suffix}-{attempt}"
        body = RUNTIME.pod(pod_name, I.NS, node, script, "image-cleanup",
                           {NAMES.key("image-digest"): digest, NAMES.key("cache-node"): node},
                           app="homestead-image-cleaner")
        for old in NAMES.find(f"/api/v1/namespaces/{I.NS}/pods", "task", "image-cleanup"):
            meta = old.get("metadata") or {}
            if meta.get("name", "").startswith(f"homestead-image-clean-{suffix}") and                     (old.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                try:
                    I.ksend("DELETE", f"/api/v1/namespaces/{I.NS}/pods/{meta['name']}")
                except urllib.error.HTTPError:
                    pass
        I.ksend("POST", f"/api/v1/namespaces/{I.NS}/pods", body)
        pods.append(pod_name)
    # The saved scan would list it until the next one; it goes from there
    # now, and a removal that fails shows up again at the next scan.
    for node in selected:
        scan = _SCANS.get(node)
        if scan:
            scan["images"] = [i for i in scan["images"]
                              if digest not in {_image_digest(ref) for ref in i.get("repoDigests") or []}]
    _save_scans()
    I._bust("imgcache")
    return {"ok": True, "digest": digest, "image": image_ref,
            "nodes": selected, "pods": pods}


import homestead_imports as I  # noqa: E402  (imports at the bottom: homestead_imports imports this module)
import homestead_container_import as CONTAINER_IMPORT  # noqa: E402
