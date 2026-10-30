"""Volumes as Homestead lists them, and the edits made to them.

Longhorn's volumes with where each copy is, what is defined to use them and
how a rebuild or restore is going; the claims on other classes; the storage
rollup the dashboard shows; and creating, growing and re-copying a volume.
Deleting one is homestead_volumes; storage classes are
homestead_storage_classes.
"""
import concurrent.futures
import re
import time
import urllib.error
import urllib.parse

import homestead_lhrebuild as LHREBUILD
import homestead_operations as OPS
import homestead_rebalance as REBALANCE
import homestead_routes
import homestead_cache as CACHE
import homestead_storage_classes as STORAGE_CLASSES
import homestead_storage_resize as STORAGE_RESIZE
import homestead_storage_workflow as STORAGE_WORKFLOW
import homestead_volume_usage as VOLUME_USAGE

kget = ksend = storage_classes = create_pvc = age_secs = runtime_check = None
_cache = {}
SYS_NS = set()
DEFAULT_NS = "lab"


def bind(_kget, _ksend, _storage_classes, _create_pvc, _age_secs, cache, system_namespaces, default_namespace,
         _runtime_check=None):
    global kget, ksend, storage_classes, create_pvc, age_secs, _cache, SYS_NS, DEFAULT_NS, runtime_check
    kget, ksend, storage_classes, create_pvc, age_secs = _kget, _ksend, _storage_classes, _create_pvc, _age_secs
    _cache, SYS_NS, DEFAULT_NS, runtime_check = cache, system_namespaces, default_namespace, _runtime_check


def _moving_copy(volume, whole=None):
    """The copies a volume keeps while Homestead moves one of them, or 0.

    Moving a copy builds the new one first, so the volume asks Longhorn for
    one more than it keeps and Longhorn calls it degraded meanwhile. With at
    least that many whole copies (when known) it is not short of anything."""
    keeps = ((volume.get("metadata") or {}).get("annotations") or {}).get(REBALANCE.MOVING) or ""
    if not str(keeps).isdigit() or str((volume.get("status") or {}).get("robustness") or "").lower() != "degraded":
        return 0
    return int(keeps) if whole is None or whole >= int(keeps) else 0


def _volume_health_reason(volume):
    """Why Longhorn is unhappy with a volume, in its own words.

    "degraded" on its own sends people to the Longhorn UI to find out what it
    means. The conditions carry the answer - most often that a replica cannot
    be scheduled because no node has room for it.
    """
    status = volume.get("status", {}) or {}
    annotations = volume.get("metadata", {}).get("annotations", {}) or {}
    conditions = []
    for condition in status.get("conditions", []) or []:
        conditions.append({"type": condition.get("type", ""),
                           "status": condition.get("status", ""),
                           "reason": condition.get("reason", ""),
                           "message": (condition.get("message") or "")[:300]})
    # Longhorn's volume conditions do not share a polarity: Scheduled is False
    # when replicas cannot be placed, while WaitForBackingImage is False in the
    # ordinary case of a volume that has no backing image to wait for. Reading
    # every False as trouble reported a detached volume as broken.
    failing = [row for row in conditions
               if (row["status"] == "False" and row["type"] == "Scheduled")
               or (row["status"] == "True" and row["type"] in ("TooManySnapshots",
                                                               "WaitForBackingImage"))]
    scheduling = annotations.get("longhorn.io/volume-scheduling-error", "") or ""
    robustness = str(status.get("robustness", "") or "").lower()
    reason = ""
    if failing:
        first = failing[0]
        reason = first["message"] or first["reason"] or f"{first['type']} is failing"
    elif scheduling:
        reason = scheduling
    elif robustness == "degraded":
        # Longhorn reports no condition while it is simply catching up.
        reason = "a replica is rebuilding; the volume is readable and writable meanwhile"
    elif robustness == "faulted":
        reason = "every replica is unusable, so the volume cannot be attached"
    return reason[:300], conditions, scheduling


def _engine_progress(engines):
    """Rebuilds and restores in flight, per volume, from Longhorn's engines.

    The volume object only says "degraded" or "restoreRequired"; how far a
    replica rebuild or a backup restore has got is on the engine, keyed by
    replica address. The slowest replica is the one the volume waits on.
    """
    out = {}
    for engine in engines:
        volume = (engine.get("spec", {}) or {}).get("volumeName") or \
            (engine.get("metadata", {}).get("labels", {}) or {}).get("longhornvolume", "")
        if not volume:
            continue
        status = engine.get("status", {}) or {}
        entry = out.setdefault(volume, {})
        rebuilding = [row for row in (status.get("rebuildStatus") or {}).values()
                      if row and (row.get("isRebuilding") or row.get("error"))]
        if rebuilding:
            entry["rebuild"] = {
                "pct": min(int(row.get("progress") or 0) for row in rebuilding),
                "replicas": len(rebuilding),
                "error": next((str(row["error"])[:300] for row in rebuilding if row.get("error")), ""),
            }
        restoring = [row for row in (status.get("restoreStatus") or {}).values()
                     if row and (row.get("isRestoring") or row.get("error"))]
        if restoring:
            entry["restore"] = {
                "pct": min(int(row.get("progress") or 0) for row in restoring),
                "error": next((str(row["error"])[:300] for row in restoring if row.get("error")), ""),
            }
    return {name: entry for name, entry in out.items() if entry}


def claim_references(pods=None):
    """Every claim something is defined to use, running or not: (namespace,
    claim) -> ["Deployment/plex", ...]. pods: the pod list a page snapshot
    cycle already read, so it is not read twice; None reads it here.

    Longhorn knows only the pods using a volume now, so a stopped container's
    volume looked the same as one nothing uses at all - and only the second
    is safe to think about deleting. A kind whose list cannot be read, or
    comes back without items or cut short, is noted under UNREAD rather than
    counted as unused; only KubeVirt being absent means no VMs. The kinds
    that were read still name what uses a claim.
    """
    refs, unread = {}, []

    def note(ns, claims, what):
        for claim in claims:
            if claim:
                refs.setdefault((ns, claim), []).append(what)

    def claims_of(podspec):
        return [(v.get("persistentVolumeClaim") or {}).get("claimName") for v in (podspec or {}).get("volumes") or []]

    def listed(kind, path, absent_ok=False):
        try:
            listing = fetched[path]
            if isinstance(listing, Exception):
                raise listing
        except urllib.error.HTTPError as error:
            if not (absent_ok and error.code == 404):
                unread.append(kind)
            return []
        except Exception:
            unread.append(kind)
            return []
        rows = listing.get("items") if isinstance(listing, dict) else None
        meta = (listing.get("metadata") or {}) if isinstance(listing, dict) else None
        if (not isinstance(rows, list) or not isinstance(meta, dict) or meta.get("continue")
                or any(not isinstance(row, dict) for row in rows)):
            unread.append(kind)
            return []
        return rows

    def each(kind, rows, read):
        # A row that is not shaped as Kubernetes writes it leaves its kind
        # unread: skipping it could leave its claim looking unused.
        try:
            for row in rows:
                read(row)
        except (KeyError, TypeError, AttributeError):
            unread.append(kind)

    def workload(kind, spec_of):
        def read(obj):
            name, ns = obj["metadata"]["name"], obj["metadata"]["namespace"]
            note(ns, claims_of(spec_of(obj)), f"{kind}/{name}")
            if kind == "StatefulSet":
                # A StatefulSet's own claims are made from its template, named
                # <template>-<set>-<n>; they belong to it too.
                for template in (obj.get("spec") or {}).get("volumeClaimTemplates") or []:
                    prefix = f"{(template.get('metadata') or {}).get('name', '')}-{name}-"
                    refs.setdefault(("__prefix__", ns, prefix), []).append(f"StatefulSet/{name}")
        return read

    def vm(obj):
        volumes = ((((obj.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or [])
        claims = [(v.get("persistentVolumeClaim") or {}).get("claimName") or (v.get("dataVolume") or {}).get("name")
                  for v in volumes]
        note(obj["metadata"]["namespace"], claims, f"VM/{obj['metadata']['name']}")

    def template(obj):
        return obj["spec"]["template"]["spec"]
    kinds = (
            ("Deployment", "/apis/apps/v1/deployments", template),
            ("StatefulSet", "/apis/apps/v1/statefulsets", template),
            ("DaemonSet", "/apis/apps/v1/daemonsets", template),
            ("CronJob", "/apis/batch/v1/cronjobs", lambda o: o["spec"]["jobTemplate"]["spec"]["template"]["spec"]))
    # Made on their own, these use a claim with nothing above them: a finished
    # bare Job or a bare Pod left its volume looking orphaned (gap 12). Made by
    # something that was read, they are already named by it.
    # A missing template is no claims here, not an unreadable kind.
    def made_template(obj):
        return ((obj.get("spec") or {}).get("template") or {}).get("spec")
    made = (
            ("Job", "/apis/batch/v1/jobs", made_template),
            ("ReplicaSet", "/apis/apps/v1/replicasets", made_template),
            ("ReplicationController", "/api/v1/replicationcontrollers", made_template),
            ("Pod", "/api/v1/pods", lambda o: o.get("spec")))
    vms = "/apis/kubevirt.io/v1/virtualmachines"
    claims_path = "/api/v1/persistentvolumeclaims"
    # The lists do not depend on each other: read at the same time (#705),
    # then judged in this order, so what counts as unread is as before.
    fetched = _read_all([path for _, path, _ in kinds] + [vms, claims_path]
                        + [path for _, path, _ in made if pods is None or path != "/api/v1/pods"])
    if pods is not None:
        fetched["/api/v1/pods"] = pods
    for kind, path, spec_of in kinds:
        each(kind, listed(kind, path), workload(kind, spec_of))
    each("VM", listed("VM", vms, absent_ok=True), vm)

    def owned_claim(obj):
        # A claim something owns is that owner's, mounted or not: a CDI
        # DataVolume's golden image waits for clones with nothing using it.
        meta = obj["metadata"]
        for owner in meta.get("ownerReferences") or []:
            note(meta["namespace"], [meta["name"]], f"{owner['kind']}/{owner['name']}")
    each("PersistentVolumeClaim", listed("PersistentVolumeClaim", claims_path), owned_claim)
    # A made row names only a claim that nothing above has named, and that its
    # own owner (a Job for its Pod, say) has not: so a Deployment's Pods or a
    # VM's launcher pod add nothing, while a claim the owner's template no
    # longer names (a CronJob edited, a rollout, a volume hotplugged into a VM
    # or added by a webhook) is still named by what mounts it. Never by
    # trusting that an owner's template matches what it made.
    above = {key: list(users) for key, users in refs.items()}
    named_by = {}

    def named_above(ns, claim):
        return bool(above.get((ns, claim))) or any(
            key[0] == "__prefix__" and key[1] == ns and claim.startswith(key[2]) for key in above)

    def bare(kind, spec_of):
        def read(obj):
            name, ns = obj["metadata"]["name"], obj["metadata"]["namespace"]
            owners = {owner.get("uid") for owner in obj["metadata"].get("ownerReferences") or []} - {None, ""}
            for claim in claims_of(spec_of(obj)):
                if not claim or named_above(ns, claim) or owners & named_by.get((ns, claim), set()):
                    continue
                note(ns, [claim], f"{kind}/{name}")
                named_by.setdefault((ns, claim), set()).add(obj["metadata"].get("uid"))
        return read
    for kind, path, spec_of in made:
        each(kind, listed(kind, path), bare(kind, spec_of))
    if unread:
        refs[UNREAD] = unread
    return refs


# claim_references' key for the kinds it could not read.
UNREAD = ("__unread__",)


def _read_all(paths):
    """Each path's answer, or the exception reading it raised, read at the same time."""
    def one(path):
        try:
            return kget(path)
        except Exception as error:  # handed back, judged by the caller as before
            return error
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as pool:
        return dict(zip(paths, pool.map(one, paths)))


def _references_for(refs, ns, claim):
    """What uses a claim. None when nothing that was read uses it but a kind
    could not be read: then it is not known to be unused."""
    found = list(refs.get((ns, claim)) or [])
    for key, owners in refs.items():
        if key[0] == "__prefix__" and key[1] == ns and claim.startswith(key[2]):
            found.extend(owners)
    if not found and refs.get(UNREAD):
        return None
    return sorted(set(found))


def other_volumes():
    """Claims on classes that are not Longhorn - k3s's local-path, NFS and
    the like - which Longhorn's list leaves out: each with its state, and why
    one is stuck when its provisioner has said."""
    longhorn = {row["name"] for row in storage_classes() if row["provisioner"] == STORAGE_CLASSES.LONGHORN_PROVISIONER}
    try:
        reasons = {}
        for event in kget("/api/v1/events?fieldSelector=reason%3DProvisioningFailed").get("items", []):
            obj = event.get("involvedObject") or {}
            if obj.get("kind") == "PersistentVolumeClaim":
                reasons[(obj.get("namespace"), obj.get("name"))] = (event.get("message") or "")[:300]
    except Exception:
        reasons = {}
    # A claim backed by a Longhorn volume is Longhorn whatever its class says:
    # restored claims name a one-off restore class that older releases removed.
    try:
        drivers = {pv["metadata"]["name"]: ((pv.get("spec") or {}).get("csi") or {}).get("driver", "")
                   for pv in kget("/api/v1/persistentvolumes").get("items", [])}
    except Exception:
        drivers = {}
    out = []
    for pvc in kget("/api/v1/persistentvolumeclaims").get("items", []):
        meta, spec, status = pvc["metadata"], pvc.get("spec") or {}, pvc.get("status") or {}
        klass = spec.get("storageClassName") or ""
        if (klass in longhorn or meta["namespace"] in SYS_NS
                or drivers.get(spec.get("volumeName") or "") == STORAGE_CLASSES.LONGHORN_PROVISIONER):
            continue
        phase = status.get("phase", "")
        out.append({"namespace": meta["namespace"], "name": meta["name"], "volume": spec.get("volumeName") or "",
                    "storage_class": klass or "(none)",
                    "phase": phase, "access_modes": spec.get("accessModes") or [],
                    "size": (status.get("capacity") or {}).get("storage")
                            or ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
                    "reason": reasons.get((meta["namespace"], meta["name"]), "") if phase != "Bound" else ""})
    return sorted(out, key=lambda row: (row["phase"] == "Bound", row["namespace"], row["name"]))


def volume_copies():
    """Longhorn volume -> where each copy of it is: host, disk folder, and the
    disk's name as Homestead set it up (/mnt/<device>), the OS disk said so."""
    base = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
    replicas = kget(f"{base}/replicas").get("items", [])
    tags = {}
    try:
        for node in kget(f"{base}/nodes").get("items", []):
            for name, disk in ((node.get("spec") or {}).get("disks") or {}).items():
                uuid = (((node.get("status") or {}).get("diskStatus") or {}).get(name) or {}).get("diskUUID")
                if uuid:
                    tags[uuid] = disk.get("tags") or []
    except Exception:
        pass
    out = {}
    # A volume with no running copy is detached. Its copies are stopped, not
    # lost: Longhorn counts one that was healthy and has not failed as whole,
    # and so does the reboot review (homestead_power) - so here too (#379).
    attached = {(r.get("spec") or {}).get("volumeName") for r in replicas
                if (r.get("status") or {}).get("currentState") == "running"}
    for r in replicas:
        spec, status = r.get("spec") or {}, r.get("status") or {}
        if not spec.get("volumeName"):
            continue
        detached = spec["volumeName"] not in attached
        path = (spec.get("diskPath") or "").rstrip("/")
        disk_tags = tags.get(spec.get("diskID"), [])
        os_disk = "os" in disk_tags or path in ("/var/lib/longhorn", "/var/lib/harvester/defaultdisk")
        label = ("OS disk" if os_disk else path.rsplit("/", 1)[-1] if path.startswith("/mnt/") else path) or "?"
        out.setdefault(spec["volumeName"], []).append({
            "node": spec.get("nodeID", ""), "path": path, "disk": label, "os": os_disk,
            "healthy": not spec.get("failedAt") and (status.get("currentState") == "running"
                                                     or detached and bool(spec.get("healthyAt"))),
            "detached": detached,
            "whole": LHREBUILD.whole(r),
            "state": status.get("currentState", "") or ("failed" if spec.get("failedAt") else "stopped")})
    for rows in out.values():
        rows.sort(key=lambda c: (c["node"], c["disk"]))
    return out


def get_volumes(pods=None):
    try:
        vols = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return []
    try:
        copies = volume_copies()
    except Exception:
        copies = {}
    try:
        refs = claim_references(pods)
    except Exception:
        refs = None
    try:
        progress = _engine_progress(
            kget("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines").get("items", []))
    except Exception:
        progress = {}
    try:
        pvcs = {(p["metadata"]["namespace"], p["metadata"]["name"]): p
                for p in kget("/api/v1/persistentvolumeclaims").get("items", [])}
    except Exception:
        pvcs = {}
    active_claims = {}
    for volume in vols:
        status = volume.get("status", {})
        ks = status.get("kubernetesStatus", {}) or {}
        key = (ks.get("namespace", ""), ks.get("pvcName", ""))
        claim = pvcs.get(key)
        if status.get("state") == "attached" and claim and claim.get("spec", {}).get("volumeName") == volume["metadata"]["name"]:
            active_claims[key] = claim
    # Cache only telemetry, keyed by claim identities/bindings; never re-label
    # an old retained copy with the replacement claim's filesystem usage.
    usage_key = "volume-filesystems:" + repr(sorted((k, p.get("metadata", {}).get("uid"), p.get("spec", {}).get("volumeName")) for k, p in active_claims.items()))
    filesystems = homestead_routes.cached(usage_key, 20, lambda: VOLUME_USAGE.collect(kget, active_claims)) if active_claims else {}
    # A cached observation must also expire if telemetry stops arriving.
    filesystems = {key: sample for key, sample in filesystems.items()
                   if -5 <= time.time() - sample["sample_at"] <= 120}
    out = []
    for v in vols:
        st = v.get("status", {})
        sp = v.get("spec", {})
        ks = st.get("kubernetesStatus", {}) or {}
        wls = ks.get("workloadsStatus") or []
        pvc_obj = pvcs.get((ks.get("namespace", ""), ks.get("pvcName", "")), {})
        pvc_spec = pvc_obj.get("spec", {}) or {}
        health_reason, conditions, scheduling_error = _volume_health_reason(v)
        # Kept after its claim went - an old copy from a storage class change,
        # or a claim deleted with its data retained. Longhorn still names the
        # claim it had, which may now be another volume's.
        unclaimed = str(ks.get("pvStatus") or "") == "Released" or bool(
            ks.get("pvcName") and not pvc_obj and pvcs) or bool(
            pvc_obj and (pvc_obj.get("spec") or {}).get("volumeName") not in ("", None, v["metadata"]["name"]))
        # Longhorn retains workload names after the final pod releases a
        # volume. These describe past use, not a current attachment. In
        # particular, retained CDI scratch PVs must still appear as unused.
        if unclaimed or ks.get("lastPodRefAt"):
            wls = []
        filesystem = filesystems.get((ks.get("namespace", ""), ks.get("pvcName", ""))) if not unclaimed and st.get("state") == "attached" else None
        moving = _moving_copy(v, len({c["node"] for c in copies.get(v["metadata"]["name"], []) if c.get("whole")}))
        if moving:
            health_reason = (f"moving a copy to another host: the new one is built first, and the volume keeps its "
                             f"{moving} whole cop{'y' if moving == 1 else 'ies'} meanwhile")
        out.append({
            "name": v["metadata"]["name"],
            "pvc_name": ks.get("pvcName", ""),
            "namespace": ks.get("namespace", ""),
            "attached": sorted({w.get("workloadName") or w.get("podName") or ""
                                for w in wls if w.get("workloadName") or w.get("podName")}),
            "attached_to": ", ".join(sorted({w.get("workloadName") or w.get("podName") or ""
                                             for w in wls if w.get("workloadName") or w.get("podName")})),
            "pod_status": ", ".join(sorted({w.get("podStatus", "") for w in wls if w.get("podStatus")})),
            "last_used": ks.get("lastPodRefAt") or ks.get("lastPVCRefAt") or "",
            "last_used_secs": age_secs(ks.get("lastPodRefAt") or ks.get("lastPVCRefAt") or ""),
            "created": v["metadata"].get("creationTimestamp", ""),
            "state": st.get("state", "?"),
            "robustness": "healthy" if moving else st.get("robustness", "?"),
            "rebalancing": bool(moving),
            "node": st.get("currentNodeID", ""),
            "size_gb": round(int(sp.get("size", 0) or 0) / 1024**3, 1),
            "replicas": moving or sp.get("numberOfReplicas", 0),
            "copies": copies.get(v["metadata"]["name"], []),
            # Detached with fewer whole copies than it asks for: Longhorn
            # repairs that only offline, or while something uses the volume.
            "copies_short": (lambda whole: {"whole": whole, "wanted": int(sp.get("numberOfReplicas") or 0),
                                            "offline": sp.get("offlineRebuilding") or "ignored"}
                             if st.get("state") == "detached" and whole < int(sp.get("numberOfReplicas") or 0) else None)(
                len({c["node"] for c in copies.get(v["metadata"]["name"], []) if c.get("whole")})),
            "engine": str(sp.get("dataEngine") or "v1").lower(),
            "actual_gb": round(int(st.get("actualSize", 0) or 0) / 1024**3, 2),
            "filesystem": filesystem,
            "used_pct": (filesystem or {}).get("used_pct"),
            "access_modes": pvc_spec.get("accessModes", []) or [],
            "storage_class": pvc_spec.get("storageClassName", ""),
            "health_reason": health_reason,
            "conditions": [row for row in conditions if row["status"] == "False"],
            "scheduling_error": scheduling_error,
            # What is defined to use it, running or not; None when that could
            # not be read, so nothing is called orphaned on a guess.
            "used_by": (_references_for(refs, ks.get("namespace", ""), ks.get("pvcName", ""))
                        if refs is not None and ks.get("pvcName") and pvc_obj and not unclaimed else None),
            "unclaimed": unclaimed,
            "rebuild": progress.get(v["metadata"]["name"], {}).get("rebuild"),
            # A restored volume keeps restoreRequired until its data is all
            # in, even between engine updates that carry no restore rows. A
            # disaster-recovery standby keeps it for good, so is left out.
            "restore": progress.get(v["metadata"]["name"], {}).get("restore") or (
                {"pct": 0, "error": ""}
                if st.get("restoreRequired") and not st.get("isStandby") else None),
        })
    return sorted(out, key=lambda x: x["name"])


def get_storage(vols=None):
    """Cluster storage rollup for the dashboard: capacity, usage, replica health.
    vols: get_volumes(), when the caller has it already (the page snapshot)."""
    vols = get_volumes() if vols is None else vols
    try:
        lhnodes = kget("/apis/longhorn.io/v1beta2/nodes").get("items", [])
    except Exception:
        lhnodes = []
    cap = avail = 0
    disks = []
    for n in lhnodes:
        for did, d in (n.get("status", {}).get("diskStatus", {}) or {}).items():
            c = d.get("storageMaximum") or 0
            a = d.get("storageAvailable") or 0
            cap += c
            avail += a
            disks.append({"node": n["metadata"]["name"],
                          "cap_gb": round(c / 1024**3, 1),
                          "avail_gb": round(a / 1024**3, 1),
                          "sched_gb": round((d.get("storageScheduled") or 0) / 1024**3, 1)})
    prov = sum(v["size_gb"] for v in vols)
    used = sum(v["actual_gb"] for v in vols)
    return {
        "cap_gb": round(cap / 1024**3, 1),
        "avail_gb": round(avail / 1024**3, 1),
        "used_gb": round((cap - avail) / 1024**3, 1),
        "used_pct": round((cap - avail) / cap * 100, 1) if cap else 0,
        "provisioned_gb": round(prov, 1),
        "actual_gb": round(used, 1),
        "volumes": len(vols),
        "healthy": len([v for v in vols if v["state"] == "attached" and v["robustness"] == "healthy"]),
        "degraded": len([v for v in vols if v["state"] == "attached" and v["robustness"] == "degraded"]),
        "faulted": len([v for v in vols if v["state"] == "attached" and v["robustness"] == "faulted"]),
        # Detached is a resting state, not a mystery: nothing is mounting the
        # volume, so there is no live replica health to report.
        "detached": len([v for v in vols if v["state"] != "attached"]),
        "unknown": len([v for v in vols if v["state"] != "attached"]),
        "attached": len([v for v in vols if v["state"] == "attached"]),
        "reasons": [{"name": v.get("pvc_name") or v["name"], "robustness": v["robustness"],
                     "reason": v["health_reason"]}
                    # Only volumes something is actually using: a detached one
                    # has no live health, so it has nothing to explain.
                    for v in vols if v.get("health_reason") and v["robustness"] != "healthy"
                    and v["state"] == "attached"][:8],
        "disks": disks,
    }


def create_volume(cfg):
    name = (cfg.get("name") or "").strip().lower()
    ns = cfg.get("namespace") or DEFAULT_NS
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", name):
        raise ValueError("volume name must be lowercase letters, numbers and dashes")
    size = int(cfg.get("size_gb", 10))
    if size < 1:
        raise ValueError("volume size must be at least 1 GiB")
    mode = cfg.get("access_mode", "ReadWriteOnce")
    if mode not in ("ReadWriteOnce", "ReadWriteMany"):
        raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
    # No class chosen: create_pvc puts it on server.py's current default.
    out = create_pvc(ns, name, size, cfg.get("storage_class") or None, mode)
    CACHE.drop_where(_cache, lambda k: k.startswith(("vol", "stor", "flow")))
    return {"ok": True, "name": name, "namespace": ns, "pvc": out}


def volume_edit_options(namespace, name, pvc=None):
    """Read current Kubernetes expansion prerequisites without changing storage."""
    if not name:
        raise ValueError("Choose a volume")
    ns = urllib.parse.quote(namespace or DEFAULT_NS, safe="")
    claim = urllib.parse.quote(name, safe="")
    if pvc is None:
        pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    spec = pvc.get("spec") or {}
    requested_mb = STORAGE_CLASSES._quantity_mb((spec.get("resources") or {}).get("requests", {}).get("storage", "0"))
    result = STORAGE_RESIZE.options(pvc, kget)
    result.update(requested_gb=-(-requested_mb // 1024) if requested_mb else 0, repair_class=False)
    if result.pop("missing_class"):
        result["reason"] = "This volume's StorageClass was removed. An administrator must repair its resize support before increasing the size."
        try:
            STORAGE_CLASSES._restore_class_repair(pvc)
            result["repair_class"] = True
        except (ValueError, urllib.error.HTTPError):
            pass
    return result


def edit_volume(cfg):
    """Grow a PVC and optionally change Longhorn replica count.

    Only what changed is sent. Longhorn's volumes live in its namespace; the
    replica count was written to a path without it, which Kubernetes answers
    "404 page not found" - after the size had already been changed, so every
    save of the form reported a failure that had half happened.
    """
    ns, name = cfg.get("namespace") or DEFAULT_NS, cfg["name"]
    pvc_path = f"/api/v1/namespaces/{urllib.parse.quote(ns, safe='')}/persistentvolumeclaims/{urllib.parse.quote(name, safe='')}"
    pvc = kget(pvc_path)
    reps = cfg.get("replicas")
    if reps is not None:
        reps = int(reps)
        if not 1 <= reps <= 5:
            raise ValueError("replica count must be between 1 and 5")
    done = []
    if cfg.get("size_gb"):
        wanted = int(cfg["size_gb"])
        requested_mb = STORAGE_CLASSES._quantity_mb(((pvc.get("spec") or {}).get("resources") or {})
                                    .get("requests", {}).get("storage", "0"))
        now_gb = -(-requested_mb // 1024) if requested_mb else 0
        if now_gb and wanted < now_gb:
            raise ValueError(f"{name} is {now_gb} GiB; volumes can grow but not shrink")
        if wanted != now_gb:
            options = volume_edit_options(ns, name, pvc)
            if not options["can_expand"]:
                raise ValueError(options["reason"])
            STORAGE_RESIZE.check_upgrade(pvc, kget)
            ksend("PATCH", pvc_path,
                  {"spec": {"resources": {"requests": {"storage": f"{wanted}Gi"}}}},
                  ctype="application/merge-patch+json")
            done.append(f"growing to {wanted} GiB")
    vol_name = pvc.get("spec", {}).get("volumeName")
    if reps is not None and vol_name:
        path = f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/{vol_name}"
        try:
            current = int(((kget(path).get("spec") or {}).get("numberOfReplicas")) or 0)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            current = None          # not a Longhorn volume: nothing to set
        if current is not None and current != reps:
            ksend("PATCH", path, {"spec": {"numberOfReplicas": reps}}, ctype="application/merge-patch+json")
            done.append(f"{reps} cop{'y' if reps == 1 else 'ies'}")
    CACHE.drop_where(_cache, lambda k: k.startswith(("vol", "stor", "flow")))
    return {"ok": True, "name": name, "detail": (f"{name}: " + ", ".join(done)) if done else f"{name} is unchanged"}


def claims(namespace):
    """A namespace's claims, for the pickers that choose one."""
    items = kget(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims")["items"]
    return [{"name": i["metadata"]["name"],
             "size": (i.get("status", {}).get("capacity", {}) or {}).get("storage") or
                     i["spec"]["resources"]["requests"]["storage"],
             "status": i.get("status", {}).get("phase", "Unknown"),
             "access_modes": i.get("spec", {}).get("accessModes", []),
             "storage_class": i.get("spec", {}).get("storageClassName", "")}
            for i in items]


def _reclass(request, step):
    """A storage class change, reviewed or started, through the journalled
    workflow; each step checks every Homestead copy can follow it."""
    cfg = {**request.body, "namespace": request.body.get("namespace") or DEFAULT_NS}
    if step == "plan":
        return STORAGE_WORKFLOW.preview(cfg, request.user, OPS, runtime_check=runtime_check)
    operation = STORAGE_WORKFLOW.start_reviewed(cfg, request.user, OPS, runtime_check=runtime_check)
    _cache.pop("vol", None)
    return {"ok": True, "operation": operation}


def _q(request, name, default=""):
    return (request.query.get(name) or [default])[0]


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/volumes"): ("viewer", lambda request: homestead_routes.page("vol", get_volumes)),
    ("GET", "/api/volumes/other"): ("viewer", lambda request: homestead_routes.cached("volother", 10, other_volumes)),
    ("GET", "/api/volumes/edit-options"): ("viewer", lambda request: volume_edit_options(
        _q(request, "ns", DEFAULT_NS), _q(request, "name"))),
    ("GET", "/api/storage"): ("viewer", lambda request: homestead_routes.page("stor", get_storage)),
    ("GET", "/api/pvcs"): ("viewer", lambda request: claims(_q(request, "ns", DEFAULT_NS))),
    ("POST", "/api/volumes/create"): ("operator", lambda request: create_volume(request.body)),
    ("POST", "/api/volumes/edit"): ("operator", lambda request: edit_volume(request.body)),
    ("POST", "/api/volumes/reclass/plan"): ("operator", lambda request: _reclass(request, "plan")),
    ("POST", "/api/volumes/reclass/start"): ("admin", lambda request: _reclass(request, "start")),
}
