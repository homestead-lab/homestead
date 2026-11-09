"""Remove an import disk from Import > Disk images (#1217).

A plain DELETE of the DataVolume isn't enough: on a Retain class its cascade leaves a
Released PV and its Longhorn volume holding the space. So the removal is a job running
the clean-up imports already have, which waits for finalizers, clears CDI's workers and
scratch and gives Retain volumes back.

It removes data, so the review names what goes, and a disk stays while anything that names
its claims uses it (a VM, a pod, even a stopped app) or an import or storage move holds it.
All of those are read again when the removal is sent. After that the job refuses a new
import or storage move, and each step reads the uses again until the DataVolume has been
asked to go. Stopping a removal that can't finish sends no more deletes.
"""
import homestead_cdi_cleanup as CDI_CLEANUP
import homestead_imports as I
import homestead_operations as OPS
import homestead_storage_guard as STORAGE_GUARD
import homestead_volumes as VOLUMES

KIND = "vm-disk-remove"
RESOURCES = CDI_CLEANUP.RESOURCES


def _dv_path(ns, name):
    return f"{CDI_CLEANUP.API}/namespaces/{ns}/datavolumes/{name}"


def _receipt(dv):
    """The clean-up's receipt for a disk that may be older than stamps: with no stamp its
    id is None, and None matches the DataVolume's missing annotation."""
    meta = dv["metadata"]
    work = CDI_CLEANUP.receipt([meta["name"]])
    work["id"] = (meta.get("annotations") or {}).get(CDI_CLEANUP.STAMP)
    work["disks"][meta["name"]]["dv_uid"] = meta.get("uid")
    return work


def _claim_names(name, work):
    return {name} | {row["name"] for row in work["disks"][name]["claims"].values()}


def _why_it_stays(ns, name, work):
    """What uses the disk now, read now. A read that fails raises: unread isn't unused."""
    why = []
    try:
        CDI_CLEANUP.observe(I.kget, ns, work)
    except ValueError as error:
        why.append(str(error))
    names = _claim_names(name, work)
    why += [f"{vm} uses it" for vm in CDI_CLEANUP.vm_users(I.kget, ns, names)]
    pods = RESOURCES.items(I.kget, f"/api/v1/namespaces/{ns}/pods")
    why += [f"{CDI_CLEANUP.ended_pod_words(pod)}: `kubectl -n {ns} delete pod {pod}`" if ended else f"Pod {pod} uses it"
            for pod, ended in CDI_CLEANUP.other_pods(pods, work, names)]
    warnings, users = [], set()
    for claim in names:
        for row in VOLUMES._consumers(ns, claim, warnings):
            # Pods and VMs are read above, a pod by uid. A Job that has ended never runs
            # again, and what it holds is its pod, as under Volumes.
            if row["kind"] in ("Pod", "VirtualMachine") or (row["kind"] == "Job" and not row["active"]):
                continue
            users.add((row["kind"], row["name"]))
    if warnings:
        raise ValueError(f"Homestead could not read what uses this disk: {warnings[0]}. Try again in a moment.")
    return why + [f"{kind} {user} uses it" for kind, user in sorted(users)]


def _hold_words(job):
    if job.get("kind") == KIND:
        return "It is already being removed"
    if job.get("status") in OPS.TERMINAL:
        return "An import is still clearing up after itself. Wait for it to end, then review this again"
    if job.get("kind") in OPS.CDI_CANCELLABLE:
        return "An import is still working on it. Cancel that import or wait for it to end, then review this again"
    return "An import is still working on it. Wait for it to end, then review this again, because this kind of import cannot be cancelled yet."


def _holds(ns, name):
    return [_hold_words(job) for job in OPS.cdi_holds(ns, name)][:1]


def _storage_holds(ns, name, work):
    """Each of its claims, volumes and Longhorn volumes, as a move's job names them."""
    disk = work["disks"][name]
    plans = [{"namespace": ns, "name": claim} for claim in sorted(_claim_names(name, work))]
    plans += [{"namespace": ns, "name": name, "pv": {"name": pv_name}, "longhorn": {"name": row["csi"].get("volumeHandle") or ""}}
              for pv_name, row in disk["pvs"].items()]
    return list(dict.fromkeys(why for row in plans for why in STORAGE_GUARD.review(row, OPS, I.kget).get("blocking_reasons", [])))


def _what_goes(ns, name, dv, work):
    disk = work["disks"][name]
    capacity = _storage(dv).get("resources", {}).get("requests", {}).get("storage", "")
    goes = [f"DataVolume {name}" + (f" ({capacity})" if capacity else "") + " and the data on it"]
    roles = {"target": "Its claim", "prime": "CDI's work claim", "scratch": "CDI's scratch claim"}
    goes += [f"{roles[row['role']]} {row['name']}" for row in disk["claims"].values()]
    goes += [f"CDI's worker pod {pod}" for pod in disk["pods"].values()]
    for pv_name in disk["pvs"]:
        pv = RESOURCES.optional(I.kget, "/api/v1/persistentvolumes/" + pv_name) or {}
        retain = (pv.get("spec") or {}).get("persistentVolumeReclaimPolicy") == "Retain"
        goes.append(f"Volume {pv_name}" + (": its Retain policy becomes Delete, so the space comes back" if retain else ""))
    return goes


def _storage(dv):
    return (dv.get("spec") or {}).get("storage") or {}


def _review(namespace, name):
    ns = I._required_name(namespace or I.NS, "namespace")
    name = I._required_name(name, "disk name")
    dv = RESOURCES.optional(I.kget, _dv_path(ns, name))
    if not dv or ((dv["metadata"].get("labels") or {}).get(I.VM_DISK_LABEL)) != "true":
        raise ValueError(f"{ns}/{name} is not a disk Homestead imported, so nothing was changed")
    work = _receipt(dv)
    blocking = _why_it_stays(ns, name, work) + _holds(ns, name) + _storage_holds(ns, name, work)
    phase = str((dv.get("status") or {}).get("phase") or "Pending")
    finished = phase == "Succeeded"
    return {"namespace": ns, "name": name, "uid": dv["metadata"].get("uid", ""), "phase": phase, "finished": finished,
            "capacity": _storage(dv).get("resources", {}).get("requests", {}).get("storage", ""),
            "storage_class": _storage(dv).get("storageClassName", ""), "blocking": blocking,
            "goes": _what_goes(ns, name, dv, work), "confirm": name,
            "note": ("This disk is finished, and a VM can still be created from it with Create VM. Removing it deletes it for good. "
                     "The source is not touched." if finished else
                     "This removes the disk, its volume claim and what CDI created for it, along with the data copied so far. "
                     "The source is not touched.")}, work


def plan(namespace, name):
    return _review(namespace, name)[0]


def remove(body, actor=""):
    """Start the removal as reviewed, under the jobs' lock so no import starts on it meanwhile."""
    with OPS.transaction():
        review, work = _review(body.get("namespace"), body.get("name"))
        ns, name = review["namespace"], review["name"]
        if str(body.get("uid") or "") != review["uid"]:
            raise ValueError("The disk changed after this review. Review it again.")
        if str(body.get("confirmation") or "").strip() != review["confirm"]:
            raise ValueError("Type the disk's name to remove it")
        if review["blocking"]:
            raise PermissionError(". ".join(review["blocking"]))
        operation = OPS.start(KIND, f"Remove disk {name}", {"kind": "DataVolume", "name": name, "namespace": ns}, "/vms/import",
                              {"namespace": ns, "name": name, "cdi_cleanup": work, "phase": "cleanup",
                               "cleanup_outcome": "succeeded", "cleanup_keep_disks": False,
                               "cleanup_detail": f"Removing disk {name}", "by": actor},
                              "Removing the disk")
    I._bust("vms", "vol")
    return {"ok": True, "operation": operation, "namespace": ns, "name": name, "message": f"Removing disk {name}"}


def status(item):
    """Until the DataVolume has been asked to go, a new use ends the job with the disk kept:
    otherwise it would sit at 99 % with no way out. After that the clean-up waits, as an
    import's does."""
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    try:
        dv = RESOURCES.optional(I.kget, _dv_path(ns, name))
        why = _why_it_stays(ns, name, ref["cdi_cleanup"]) if dv and not dv["metadata"].get("deletionTimestamp") else []
    except Exception:
        ref["retain_resources"] = True
        return "running", 99, f"Removing disk {name}. Kubernetes could not confirm that nothing uses it"
    if why:
        detail = f"{'. '.join(why)}. Disk {name} and its data were kept"
        ref.update(phase="failed", detail=detail, retain_resources=False)
        return "failed", 100, detail
    state, progress, message = CDI_CLEANUP.finish(item, I.kget, I.ksend, OPS.checkpoint)
    if state == "succeeded":
        message = ref["detail"] = f"Disk {name} removed, and its storage given back"
    return state, progress, message


def _left(ref):
    """What of the disk is there now, as (being deleted, still there), or None when that
    can't be read. Once the DataVolume has been asked to go, its claims follow it, and so
    does a volume on the Delete policy."""
    ns, name = ref["namespace"], ref["name"]
    work = ref["cdi_cleanup"]
    try:
        dv = RESOURCES.optional(I.kget, _dv_path(ns, name))
        going = not dv or bool(dv["metadata"].get("deletionTimestamp"))
        deleting, staying = [], []
        if dv:
            (deleting if going else staying).append(f"DataVolume {name}")
        for claim in sorted(_claim_names(name, work)):
            row = RESOURCES.optional(I.kget, f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
            if row:
                (deleting if going or row["metadata"].get("deletionTimestamp") else staying).append(f"claim {claim}")
        for pv_name in work["disks"][name]["pvs"]:
            row = RESOURCES.optional(I.kget, "/api/v1/persistentvolumes/" + pv_name)
            if row:
                follows = going and (row.get("spec") or {}).get("persistentVolumeReclaimPolicy") == "Delete"
                (deleting if follows or row["metadata"].get("deletionTimestamp") else staying).append(f"volume {pv_name}")
    except Exception:
        return None
    return deleting, staying


def _left_words(left):
    if left is None:
        return "Homestead could not read what is left. Check under Import > Disk images and Volumes."
    deleting, staying = left
    if not deleting and not staying:
        return "Nothing of the disk is left"
    return " ".join(([f"Being deleted, and Kubernetes will finish this: {', '.join(deleting)}."] if deleting else []) +
                    ([f"Still there: {', '.join(staying)}. Check them under Import > Disk images and Volumes"] if staying else []))


def cancel_plan(item):
    return {"mode": "forget", "action": "Stop following it", "severity": "medium", "needs": "admin",
            "keeps": ["Homestead stops following this removal and sends no more deletes", _left_words(_left(item["ref"]))]}


def cancel_run(item, _options):
    """A way out for a removal a safety check has stopped. It only reads, and it can't raise,
    so the name is let go only together with the message saying what is left."""
    ref = item["ref"]
    words = _left_words(_left(ref))
    ref.update(phase="failed", retain_resources=False)
    return f"Stopped following. Homestead sends no more deletes. {words}"


ROUTES = {
    ("POST", "/api/vm-disks/remove-plan"): ("admin", lambda request: plan(request.body.get("namespace"), request.body.get("name"))),
    ("POST", "/api/vm-disks/remove"): ("admin", lambda request: remove(request.body, request.user or "")),
}

OPS.RESOLVERS[KIND] = status
OPS.CANCELLERS[KIND] = (cancel_plan, cancel_run)
OPS.CDI_CANCELLABLE.add(KIND)
